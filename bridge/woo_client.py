"""Minimal WooCommerce REST API (v3) client.

Authentication uses HTTP Basic auth with a consumer key/secret, which WooCommerce
accepts over HTTPS. Create keys in: WooCommerce > Settings > Advanced > REST API
(permission "Read" is enough for this bridge).
"""
import logging
from typing import Any, Dict, List, Optional

import requests

from . import config
from .http import make_session

log = logging.getLogger(__name__)


class WooError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class WooClient:
    def __init__(self, base_url: str = None, key: str = None, secret: str = None, session=None):
        self.base_url = (base_url or config.WC_URL).rstrip("/")
        self.key = key or config.WC_CONSUMER_KEY
        self.secret = secret or config.WC_CONSUMER_SECRET
        if not (self.base_url and self.key and self.secret):
            raise WooError("WooCommerce API is not configured (WC_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET).")
        if self.base_url.startswith("http://"):
            log.warning("WC_URL uses http://. WooCommerce only accepts key/secret auth over HTTPS in production.")
        self.session = session or make_session()
        self.session.auth = (self.key, self.secret)

    # -- low level --------------------------------------------------------------
    def _url(self, path: str, namespace: str = "wc/v3") -> str:
        return f"{self.base_url}/wp-json/{namespace}/{path.lstrip('/')}"

    def _get_raw(self, path: str, params: Dict = None, namespace: str = "wc/v3") -> requests.Response:
        try:
            r = self.session.get(self._url(path, namespace), params=params or {}, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise WooError(f"Network error talking to WooCommerce: {exc}") from exc
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise WooError(f"WooCommerce {r.status_code}: {msg}", r.status_code)
        return r

    def get(self, path: str, params: Dict = None, namespace: str = "wc/v3") -> Any:
        return self._get_raw(path, params, namespace).json()

    def get_all(self, path: str, params: Dict = None, max_pages: int = 200) -> List[Dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page, items = 1, []
        while page <= max_pages:
            params["page"] = page
            r = self._get_raw(path, params)
            batch = r.json()
            items.extend(batch)
            total_pages = int(r.headers.get("X-WP-TotalPages", "1") or 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return items

    # -- products ---------------------------------------------------------------
    PRODUCT_LIST_FIELDS = "id,name,slug,permalink,categories,price,regular_price,sale_price,stock_status,type,sku"

    def all_products(self) -> List[Dict]:
        return self.get_all("products", {"status": "publish", "_fields": self.PRODUCT_LIST_FIELDS})

    def all_categories(self) -> List[Dict]:
        return self.get_all("products/categories", {"_fields": "id,name,slug,count,parent"})

    def search_products(self, query: str, limit: int = 5) -> List[Dict]:
        return self.get("products", {"search": query, "status": "publish", "per_page": limit})

    def get_product(self, product_id: int) -> Dict:
        return self.get(f"products/{int(product_id)}")

    def get_product_by_slug(self, slug: str) -> Optional[Dict]:
        res = self.get("products", {"slug": slug})
        return res[0] if res else None

    def get_variations(self, product_id: int) -> List[Dict]:
        return self.get(f"products/{int(product_id)}/variations", {"per_page": 50})

    # -- orders -----------------------------------------------------------------
    def find_order(self, order_number: str) -> Optional[Dict]:
        """Find an order by the number the customer sees.

        Tries the order ID first, then a search (covers "Sequential Order Number"
        style plugins where the visible number differs from the ID).
        """
        num = str(order_number).strip().lstrip("#")
        if num.isdigit():
            try:
                order = self.get(f"orders/{int(num)}")
                if str(order.get("number", order.get("id"))) in (num, str(order.get("id"))):
                    return order
            except WooError as exc:
                if exc.status not in (404, 400):
                    raise
        try:
            for o in self.get("orders", {"search": num, "per_page": 10}):
                if str(o.get("number")) == num:
                    return o
        except WooError as exc:
            if exc.status not in (404, 400):
                raise
        return None

    def order_notes(self, order_id: int, customer_only: bool = True) -> List[Dict]:
        params = {"type": "customer"} if customer_only else {}
        return self.get(f"orders/{int(order_id)}/notes", params)

    def order_refunds(self, order_id: int) -> List[Dict]:
        return self.get(f"orders/{int(order_id)}/refunds")

    def shipment_tracking(self, order: Dict) -> List[Dict]:
        """Tracking from the official 'Shipment Tracking' extension, if installed."""
        # 1) Meta data (works for several tracking plugins)
        for m in order.get("meta_data", []):
            if m.get("key") in ("_wc_shipment_tracking_items", "_aftership_tracking_items") and isinstance(m.get("value"), list):
                return m["value"]
        # 2) Extension REST endpoint
        try:
            return self.get(
                f"orders/{int(order['id'])}/shipment-trackings", namespace="wc-shipment-tracking/v3"
            )
        except WooError:
            return []
