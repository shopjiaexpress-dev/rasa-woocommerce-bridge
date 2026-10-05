"""A tiny fake WooCommerce store (sitemap + REST API + pages) for local testing.

    python tests/mock_woo_server.py 8099
    WC_URL=http://localhost:8099 WC_CONSUMER_KEY=ck_test WC_CONSUMER_SECRET=cs_test python -m bridge.sync
"""
import base64
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY, SECRET = "ck_test", "cs_test"
NOW = datetime.now(timezone.utc)

PRODUCTS = [
    {"id": 11, "name": "Classic Cotton Hoodie", "slug": "classic-cotton-hoodie", "price": "1499", "regular_price": "1999",
     "sale_price": "1499", "on_sale": True, "stock_status": "instock", "stock_quantity": 4, "type": "variable", "sku": "HD-01",
     "categories": [{"id": 2, "name": "Hoodies"}], "short_description": "<p>Soft brushed cotton hoodie with kangaroo pocket.</p>",
     "attributes": [{"name": "Size", "options": ["S", "M", "L", "XL"], "visible": True}, {"name": "Color", "options": ["Black", "Grey"], "visible": True}]},
    {"id": 12, "name": "Trail Running Shoes", "slug": "trail-running-shoes", "price": "3999", "regular_price": "3999",
     "sale_price": "", "on_sale": False, "stock_status": "instock", "stock_quantity": 25, "type": "simple", "sku": "SH-12",
     "categories": [{"id": 3, "name": "Shoes"}], "short_description": "<p>Grippy, lightweight shoes for off-road running.</p>", "attributes": []},
    {"id": 13, "name": "Leather Wallet", "slug": "leather-wallet", "price": "899", "regular_price": "899", "sale_price": "",
     "on_sale": False, "stock_status": "outofstock", "stock_quantity": 0, "type": "simple", "sku": "WL-13",
     "categories": [{"id": 4, "name": "Accessories"}], "short_description": "Genuine leather bi-fold wallet.", "attributes": []},
    {"id": 14, "name": "Zip Hoodie Grey", "slug": "zip-hoodie-grey", "price": "1799", "regular_price": "1799", "sale_price": "",
     "on_sale": False, "stock_status": "instock", "stock_quantity": 40, "type": "simple", "sku": "HD-02",
     "categories": [{"id": 2, "name": "Hoodies"}], "short_description": "Full-zip hoodie.", "attributes": []},
]
for p in PRODUCTS:
    p["permalink"] = f"/product/{p['slug']}/"
CATEGORIES = [
    {"id": 2, "name": "Hoodies", "slug": "hoodies", "count": 2, "parent": 0},
    {"id": 3, "name": "Shoes", "slug": "shoes", "count": 1, "parent": 0},
    {"id": 4, "name": "Accessories", "slug": "accessories", "count": 1, "parent": 0},
]


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


ORDERS = {
    1001: {"id": 1001, "number": "1001", "status": "processing", "currency": "INR", "currency_symbol": "₹", "total": "3999.00",
           "date_created": iso(NOW - timedelta(days=2)), "date_completed": None, "date_modified": iso(NOW),
           "billing": {"email": "anita@example.com"}, "line_items": [{"name": "Trail Running Shoes", "quantity": 1}],
           "shipping_lines": [{"method_title": "Express Shipping"}],
           "meta_data": [{"key": "_delivery_date", "value": (NOW + timedelta(days=3)).strftime("%Y-%m-%d")}]},
    1002: {"id": 1002, "number": "1002", "status": "completed", "currency": "INR", "currency_symbol": "₹", "total": "1499.00",
           "date_created": iso(NOW - timedelta(days=12)), "date_completed": iso(NOW - timedelta(days=9)), "date_modified": iso(NOW),
           "billing": {"email": "rahul@example.com"}, "line_items": [{"name": "Classic Cotton Hoodie", "quantity": 1}],
           "shipping_lines": [{"method_title": "Standard"}],
           "meta_data": [{"key": "_wc_shipment_tracking_items", "value": [
               {"tracking_provider": "Delhivery", "tracking_number": "DLV123456", "date_shipped": str(int((NOW - timedelta(days=11)).timestamp())),
                "custom_tracking_link": "https://track.example/DLV123456"}]}]},
    1003: {"id": 1003, "number": "1003", "status": "refunded", "currency": "INR", "currency_symbol": "₹", "total": "899.00",
           "date_created": iso(NOW - timedelta(days=40)), "date_completed": iso(NOW - timedelta(days=38)), "date_modified": iso(NOW),
           "billing": {"email": "sam@example.org"}, "line_items": [{"name": "Leather Wallet", "quantity": 1}],
           "shipping_lines": [], "meta_data": []},
}
NOTES = {1001: [{"note": "Your order has been packed and will ship tomorrow.", "date_created": iso(NOW - timedelta(hours=5))}]}
REFUNDS = {1003: [{"amount": "899.00", "reason": "Item damaged", "date_created": iso(NOW - timedelta(days=30))}]}

PAGES = {
    "/shipping-policy/": ("Shipping Policy", "<h2>Delivery times</h2><p>We ship all orders within 1-2 business days. Standard delivery takes 4-7 business days across India and express delivery takes 2-3 days.</p><h2>Shipping charges</h2><p>Shipping is free on all orders above Rs 999. Orders below that are charged a flat Rs 79 shipping fee.</p>"),
    "/returns-and-refunds/": ("Returns and Refunds", "<h2>Return policy</h2><p>You can return any unused item within 30 days of delivery for a full refund or exchange. Items must be in original packaging with tags attached.</p><h2>Refund timeline</h2><p>Refunds are processed to the original payment method within 5-7 business days after we receive the returned item.</p>"),
    "/contact-us/": ("Contact Us", "<p>You can reach our support team by email at support@example.com or call +91 98765 43210, Monday to Saturday from 10am to 7pm IST.</p>"),
    "/about/": ("About Us", "<p>We are a small Indian clothing brand making comfortable everyday wear from organic cotton since 2015.</p>"),
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def base(self):
        return f"http://{self.headers['Host']}"

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        b = self.base()
        if path == "/sitemap_index.xml":
            items = "".join(f"<sitemap><loc>{b}/{n}</loc></sitemap>" for n in ["product-sitemap.xml", "product_cat-sitemap.xml", "page-sitemap.xml"])
            return self._send(200, f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>', "application/xml")
        if path.endswith("-sitemap.xml"):
            kind = path.strip("/").replace("-sitemap.xml", "")
            if kind == "product":
                locs = [f"{b}/product/{p['slug']}/" for p in PRODUCTS]
            elif kind == "product_cat":
                locs = [f"{b}/product-category/{c['slug']}/" for c in CATEGORIES]
            else:
                locs = [f"{b}{p}" for p in PAGES]
            urls = "".join(f"<url><loc>{l}</loc><lastmod>2026-10-01</lastmod></url>" for l in locs)
            return self._send(200, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', "application/xml")
        if path in PAGES:
            title, body = PAGES[path]
            html = f"<html><head><title>{title} | Demo Store</title></head><body><header><nav>Home Shop Cart</nav></header><main><h1>{title}</h1>{body}</main><footer>© Demo</footer></body></html>"
            return self._send(200, html, "text/html; charset=utf-8")

        if path.startswith("/wp-json/"):
            auth = self.headers.get("Authorization", "")
            if auth != "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode():
                return self._send(401, {"code": "woocommerce_rest_cannot_view", "message": "Sorry, you cannot list resources."})
            return self.api(path, q, b)
        return self._send(404, "not found", "text/plain")

    def api(self, path, q, b):
        def prod(p):
            d = dict(p)
            d["permalink"] = b + p["permalink"]
            return d

        if path == "/wp-json/wc/v3/products":
            res = [prod(p) for p in PRODUCTS]
            if "slug" in q:
                res = [p for p in res if p["slug"] == q["slug"]]
            if "search" in q:
                terms = q["search"].lower().split()
                res = [p for p in res if all(t in p["name"].lower() for t in terms)]
            per, page = int(q.get("per_page", 10)), int(q.get("page", 1))
            total_pages = max(1, -(-len(res) // per))
            return self._send(200, res[(page - 1) * per: page * per], headers={"X-WP-TotalPages": total_pages, "X-WP-Total": len(res)})
        m = re.fullmatch(r"/wp-json/wc/v3/products/(\d+)", path)
        if m:
            p = next((p for p in PRODUCTS if p["id"] == int(m.group(1))), None)
            return self._send(200, prod(p)) if p else self._send(404, {"message": "Invalid ID."})
        if path == "/wp-json/wc/v3/products/categories":
            return self._send(200, CATEGORIES, headers={"X-WP-TotalPages": 1})
        if path == "/wp-json/wc/v3/orders":
            s = q.get("search", "")
            return self._send(200, [o for o in ORDERS.values() if s in (o["number"], o["billing"]["email"])])
        m = re.fullmatch(r"/wp-json/wc/v3/orders/(\d+)(/notes|/refunds)?", path)
        if m:
            oid = int(m.group(1))
            if oid not in ORDERS:
                return self._send(404, {"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})
            if m.group(2) == "/notes":
                return self._send(200, NOTES.get(oid, []))
            if m.group(2) == "/refunds":
                return self._send(200, REFUNDS.get(oid, []))
            return self._send(200, ORDERS[oid])
        if path.startswith("/wp-json/wc-shipment-tracking/"):
            return self._send(404, {"code": "rest_no_route", "message": "No route"})
        return self._send(404, {"code": "rest_no_route", "message": "No route"})


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"Mock WooCommerce on http://localhost:{port}")
    serve(port).serve_forever()
