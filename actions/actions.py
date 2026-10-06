"""Rasa custom actions that talk to WooCommerce through the bridge package."""
import html
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Text

from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.types import DomainDict

# Make the sibling `bridge` package importable however the action server is started.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import config, store  # noqa: E402
from bridge.catalog import clean_query  # noqa: E402
from bridge.woo_client import WooClient, WooError  # noqa: E402

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[\w.+\-']+@[\w\-]+(\.[\w\-]+)+$")
STATUS_TEXT = {
    "pending": "awaiting payment",
    "processing": "being prepared for shipping",
    "on-hold": "on hold (we're waiting for payment confirmation)",
    "completed": "completed / shipped",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "failed": "failed (payment didn't go through)",
    "checkout-draft": "a draft that was never placed",
}

_woo: Optional[WooClient] = None


def woo() -> Optional[WooClient]:
    global _woo
    if _woo is None and config.woo_api_configured():
        _woo = WooClient()
    return _woo


def _strip(s: str, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _money(amount, currency: str = "") -> str:
    if amount in (None, ""):
        return ""
    return f"{currency} {amount}".strip()


def _date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if str(s).isdigit():  # unix timestamp (e.g. Order Delivery Date plugin)
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_date(d: Optional[datetime]) -> str:
    return d.strftime("%d %b %Y") if d else "unknown"


def _entity(tracker: Tracker, *names: str) -> Optional[str]:
    for n in names:
        v = next(tracker.get_latest_entity_values(n), None)
        if v:
            return v
    return None


# =============================================================================
# Products
# =============================================================================
class ActionSearchProducts(Action):
    def name(self) -> Text:
        return "action_search_products"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[Dict]:
        text = tracker.latest_message.get("text", "")
        product = _entity(tracker, "product")
        category_ent = _entity(tracker, "product_category")
        query = product or category_ent or text
        cat = store.catalog()

        # Category question ("what t-shirts do you have") -> category link + top items
        category = cat.find_category(category_ent or query) if not product else None
        results = cat.search(query, limit=5)
        if category:
            in_cat = [p for p in cat.products if category["name"] in p.get("categories", [])][:5]
            results = in_cat or results

        # Fall back to the live API search when the cached catalogue has no hit.
        if not results and woo():
            try:
                results = [
                    {"id": p["id"], "name": _strip(p["name"]), "url": cat.public_url(p), "price": p.get("price"),
                     "stock_status": p.get("stock_status")}
                    for p in woo().search_products(clean_query(query), limit=5)
                ]
            except WooError as exc:
                log.warning("live product search failed: %s", exc)

        if not results:
            dispatcher.utter_message(response="utter_no_products_found")
            return [SlotSet("current_product_id", None)]

        lines = []
        if category and category.get("url"):
            lines.append(f"Here's what we have in **{category['name']}** ({category['url']}):")
        else:
            lines.append("Here's what I found:")
        for p in results:
            price = f" — {p['price']}" if p.get("price") else ""
            stock = " (out of stock)" if p.get("stock_status") == "outofstock" else ""
            lines.append(f"• [{p['name']}]({p['url']}){price}{stock}")
        if len(results) == 1 or not category:
            lines.append("Ask me about any of these for price, stock and details.")
        dispatcher.utter_message(text="\n".join(lines))

        first = results[0]
        return [
            SlotSet("current_product_id", str(first["id"]) if first.get("id") else None),
            SlotSet("current_product_name", first["name"]),
        ]


class ActionProductDetails(Action):
    def name(self) -> Text:
        return "action_product_details"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[Dict]:
        cat = store.catalog()
        named = _entity(tracker, "product")
        cached = None
        if named:
            hits = cat.search(named, limit=1)
            cached = hits[0] if hits else None
        if cached is None:
            # "how much is it?" -> use the product we last talked about
            pid = tracker.get_slot("current_product_id")
            cached = cat.get(product_id=pid) if pid else None
        if cached is None:
            hits = cat.search(tracker.latest_message.get("text", ""), limit=1)
            cached = hits[0] if hits else None

        product = None
        if woo():
            try:
                if cached is not None:
                    product = woo().get_product(cached["id"]) if cached.get("id") else woo().get_product_by_slug(cached["slug"])
                elif tracker.get_slot("current_product_id"):
                    # Not in the local catalogue (e.g. sync still running): ask WooCommerce directly.
                    product = woo().get_product(tracker.get_slot("current_product_id"))
                elif named:
                    found = woo().search_products(clean_query(named), limit=1)
                    product = found[0] if found else None
            except WooError as exc:
                log.warning("product fetch failed: %s", exc)
        if cached is None and product is None:
            dispatcher.utter_message(response="utter_ask_which_product")
            return []

        if not product:  # sitemap-only mode
            dispatcher.utter_message(text=f"**{cached['name']}** — see full details here: {cached['url']}")
            return [SlotSet("current_product_name", cached["name"])]

        lines = [f"**{_strip(product['name'])}**"]
        if product.get("on_sale") and product.get("regular_price"):
            lines.append(f"Price: {product['price']} (was {product['regular_price']})")
        elif product.get("price"):
            lines.append(f"Price: {product['price']}")
        stock = product.get("stock_status")
        if stock == "instock":
            qty = product.get("stock_quantity")
            lines.append(f"In stock{f' ({qty} left)' if qty and qty < 10 else ''}")
        elif stock == "onbackorder":
            lines.append("Available on backorder")
        elif stock == "outofstock":
            lines.append("Currently out of stock")
        desc = _strip(product.get("short_description") or product.get("description"), 280)
        if desc:
            lines.append(desc)
        attrs = [
            f"{a['name']}: {', '.join(a.get('options', []))}"
            for a in product.get("attributes", [])
            if a.get("visible", True) and a.get("options")
        ]
        if attrs:
            lines.append("Options — " + "; ".join(attrs))
        lines.append(f"View / buy: {cat.public_url(product)}")
        dispatcher.utter_message(text="\n".join(lines))
        return [SlotSet("current_product_id", str(product["id"])), SlotSet("current_product_name", _strip(product["name"]))]


# =============================================================================
# Site pages / FAQ (crawled from the sitemap)
# =============================================================================
class ActionAnswerFromSite(Action):
    def name(self) -> Text:
        return "action_answer_from_site"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[Dict]:
        question = tracker.latest_message.get("text", "")
        hits = store.pages().search(question, k=2)
        if not hits or hits[0][0] < config.PAGE_ANSWER_MIN_SCORE:
            # Maybe it was a product question that NLU didn't recognise.
            products = store.catalog().search(question, limit=3, min_score=80)
            if products:
                links = "\n".join(f"• [{p['name']}]({p['url']})" for p in products)
                dispatcher.utter_message(text=f"These products might be what you're looking for:\n{links}")
                return []
            dispatcher.utter_message(response="utter_default")
            return []
        score, chunk = hits[0]
        snippet = _strip(chunk["text"], 450)
        heading = chunk["heading"] if chunk["heading"] != chunk["title"] else ""
        title = f"{chunk['title']} — {heading}" if heading else chunk["title"]
        dispatcher.utter_message(text=f"{snippet}\n_From: [{title}]({chunk['url']})_")
        return []


# =============================================================================
# Orders: status, delivery date, returns/refunds (email-verified)
# =============================================================================
class ValidateOrderForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_order_form"

    def validate_order_number(self, value: Any, dispatcher, tracker, domain) -> Dict[Text, Any]:
        if value is None:  # slot was just cleared (e.g. "another order")
            return {"order_number": None}
        m = re.search(r"\d{2,12}", str(value or ""))
        if not m:
            dispatcher.utter_message(response="utter_invalid_order_number")
            return {"order_number": None}
        return {"order_number": m.group(0)}

    def validate_order_email(self, value: Any, dispatcher, tracker, domain) -> Dict[Text, Any]:
        if value is None:
            return {"order_email": None}
        m = re.search(r"[\w.+\-']+@[\w\-]+(\.[\w\-]+)+", str(value or ""))
        if not m or not EMAIL_RE.match(m.group(0)):
            dispatcher.utter_message(response="utter_invalid_email")
            return {"order_email": None}
        return {"order_email": m.group(0).lower()}


class ActionOrderInfo(Action):
    """Runs after order_form. Verifies the email against the order's billing email
    before revealing anything, then answers based on `order_topic`."""

    def name(self) -> Text:
        return "action_order_info"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[Dict]:
        attempts = int(tracker.get_slot("order_attempts") or 0)
        if attempts >= config.MAX_ORDER_VERIFY_ATTEMPTS:
            dispatcher.utter_message(response="utter_too_many_attempts")
            return [SlotSet("order_number", None), SlotSet("order_email", None)]
        if not woo():
            dispatcher.utter_message(response="utter_orders_unavailable")
            return []

        number, email = tracker.get_slot("order_number"), (tracker.get_slot("order_email") or "").lower()
        try:
            order = woo().find_order(number)
        except WooError as exc:
            log.error("order lookup failed: %s", exc)
            dispatcher.utter_message(response="utter_orders_unavailable")
            return []

        billing_email = ((order or {}).get("billing") or {}).get("email", "").lower()
        if not order or not billing_email or billing_email != email:
            # Same message whether the order doesn't exist or the email is wrong.
            dispatcher.utter_message(response="utter_order_not_found")
            return [SlotSet("order_attempts", attempts + 1), SlotSet("order_number", None), SlotSet("order_email", None)]

        topic = tracker.get_slot("order_topic") or "status"
        if topic == "delivery":
            text = self.delivery(order)
        elif topic == "return":
            text = self.returns(order)
        else:
            text = self.status(order) + "\n" + self.delivery(order, short=True)
        dispatcher.utter_message(text=text.strip())
        return [SlotSet("order_attempts", 0), SlotSet("order_verified", True)]

    # -- sections ---------------------------------------------------------------
    @staticmethod
    def status(order: Dict) -> str:
        cur = order.get("currency_symbol") or order.get("currency", "")
        items = ", ".join(f"{i['quantity']}× {_strip(i['name'], 60)}" for i in order.get("line_items", []))
        lines = [
            f"Order **#{order.get('number', order['id'])}** placed on {_fmt_date(_date(order.get('date_created')))} "
            f"is **{STATUS_TEXT.get(order['status'], order['status'])}**.",
            f"Items: {items}" if items else "",
            f"Total: {_money(order.get('total'), cur)}",
        ]
        ship = ", ".join(s.get("method_title", "") for s in order.get("shipping_lines", []) if s.get("method_title"))
        if ship:
            lines.append(f"Shipping method: {ship}")
        try:
            notes = woo().order_notes(order["id"])
            if notes:
                latest = sorted(notes, key=lambda n: n.get("date_created", ""))[-1]
                lines.append(f"Latest update: {_strip(latest.get('note'), 200)}")
        except WooError:
            pass
        return "\n".join(l for l in lines if l)

    @staticmethod
    def delivery(order: Dict, short: bool = False) -> str:
        lines = []
        tracking = woo().shipment_tracking(order)
        for t in tracking:
            provider = t.get("formatted_tracking_provider") or t.get("tracking_provider") or t.get("custom_tracking_provider") or "Carrier"
            link = t.get("formatted_tracking_link") or t.get("custom_tracking_link") or ""
            shipped = _date(t.get("date_shipped"))
            lines.append(
                f"📦 {provider} tracking **{t.get('tracking_number')}**"
                + (f", shipped {_fmt_date(shipped)}" if shipped else "")
                + (f" — track: {link}" if link else "")
            )
        meta = {m.get("key"): m.get("value") for m in order.get("meta_data", [])}
        for key in config.DELIVERY_DATE_META_KEYS:
            if meta.get(key):
                d = _date(meta[key])
                lines.append(f"Expected delivery date: **{_fmt_date(d) if d else meta[key]}**")
                break
        if order["status"] == "completed" and order.get("date_completed"):
            lines.append(f"The order was marked complete on {_fmt_date(_date(order['date_completed']))}.")
        if not lines:
            if short:
                return ""
            if order["status"] in ("processing", "on-hold", "pending"):
                return "Your order hasn't shipped yet, so there's no tracking number or delivery date yet. You'll get an email as soon as it ships."
            return "I don't have tracking or delivery-date details for this order. Please contact our support team."
        return "\n".join(lines)

    @staticmethod
    def returns(order: Dict) -> str:
        cur = order.get("currency_symbol") or order.get("currency", "")
        lines = []
        try:
            refunds = woo().order_refunds(order["id"])
        except WooError:
            refunds = []
        if refunds:
            for r in refunds:
                amt = str(r.get("amount", "")).lstrip("-")
                reason = f" — {r['reason']}" if r.get("reason") else ""
                lines.append(f"Refund of {_money(amt, cur)} issued on {_fmt_date(_date(r.get('date_created')))}{reason}.")
        if order["status"] == "refunded":
            lines.append("This order has been fully refunded.")
        elif order["status"] == "completed":
            done = _date(order.get("date_completed") or order.get("date_modified"))
            if done:
                if done.tzinfo is None:
                    done = done.replace(tzinfo=timezone.utc)
                deadline = done + timedelta(days=config.RETURN_WINDOW_DAYS)
                if datetime.now(timezone.utc) <= deadline:
                    lines.append(f"✅ This order is eligible for return until **{_fmt_date(deadline)}** ({config.RETURN_WINDOW_DAYS}-day window).")
                else:
                    lines.append(f"The {config.RETURN_WINDOW_DAYS}-day return window for this order ended on {_fmt_date(deadline)}.")
        elif order["status"] in ("processing", "pending", "on-hold"):
            lines.append("This order hasn't shipped yet — if you'd like to cancel or change it, please contact us as soon as possible.")
        elif order["status"] == "cancelled":
            lines.append("This order was cancelled.")
        # Point to the store's own returns policy page (from the crawled sitemap).
        hits = store.pages().search("return refund policy exchange", k=1)
        if hits and hits[0][0] >= config.PAGE_ANSWER_MIN_SCORE:
            lines.append(f"Our return policy: {hits[0][1]['url']}")
        return "\n".join(lines) or "I couldn't find return details for this order."


class ActionResetOrder(Action):
    def name(self) -> Text:
        return "action_reset_order"

    def run(self, dispatcher, tracker, domain) -> List[Dict]:
        return [SlotSet("order_number", None), SlotSet("order_email", None), SlotSet("order_verified", False)]
