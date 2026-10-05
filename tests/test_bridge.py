"""End-to-end test of sync + custom actions against the mock store.

    pip install -r requirements-actions.txt pytest
    pytest -q tests/
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PORT = int(os.getenv("MOCK_PORT", "8098"))
TMP = Path(tempfile.mkdtemp())
os.environ.update(
    WC_URL=f"http://127.0.0.1:{PORT}", WC_CONSUMER_KEY="ck_test", WC_CONSUMER_SECRET="cs_test",
    SITEMAP_URL="", CACHE_DIR=str(TMP / "cache"), LOOKUP_FILE=str(TMP / "lookups.yml"),
)

import mock_woo_server  # noqa: E402

_srv = mock_woo_server.serve(PORT)
threading.Thread(target=_srv.serve_forever, daemon=True).start()

from rasa_sdk import Tracker  # noqa: E402
from rasa_sdk.executor import CollectingDispatcher  # noqa: E402

from bridge import sync  # noqa: E402
from actions import actions as A  # noqa: E402

REPORT = sync.run()


def tracker(text="", intent="", entities=None, slots=None):
    return Tracker(
        sender_id="t", slots=slots or {},
        latest_message={"text": text, "intent": {"name": intent}, "entities": entities or []},
        events=[], paused=False, followup_action=None, active_loop={}, latest_action_name=None,
    )


def run(action, **kw):
    d = CollectingDispatcher()
    events = action.run(d, tracker(**kw), {})
    text = "\n".join(m.get("text") or m.get("response") or "" for m in d.messages)
    return text, {e["name"]: e["value"] for e in events if e.get("event") == "slot"}


# ---- sync -------------------------------------------------------------------
def test_sync_report():
    assert REPORT["sitemap"].endswith("/sitemap_index.xml")
    assert REPORT["products"] == 4 and REPORT["categories"] == 3
    assert REPORT["page_chunks"] >= 4
    lookups = Path(os.environ["LOOKUP_FILE"]).read_text()
    assert "- lookup: product" in lookups and "classic cotton hoodie" in lookups
    assert "- lookup: product_category" in lookups and "hoodies" in lookups


# ---- products ---------------------------------------------------------------
def test_search_by_entity():
    text, slots = run(A.ActionSearchProducts(), text="do you have running shoes",
                      entities=[{"entity": "product", "value": "running shoes"}])
    assert "Trail Running Shoes" in text and "/product/trail-running-shoes/" in text
    assert slots["current_product_id"] == "12"


def test_search_by_category():
    text, _ = run(A.ActionSearchProducts(), text="what hoodies do you have",
                  entities=[{"entity": "product_category", "value": "hoodies"}])
    assert "Classic Cotton Hoodie" in text and "Zip Hoodie Grey" in text and "Hoodies" in text


def test_search_free_text_and_out_of_stock():
    text, _ = run(A.ActionSearchProducts(), text="I'm looking for a leather wallet")
    assert "Leather Wallet" in text and "out of stock" in text


def test_search_nothing():
    text, _ = run(A.ActionSearchProducts(), text="do you sell tractors")
    assert text == "utter_no_products_found"


def test_product_details_from_slot():
    text, _ = run(A.ActionProductDetails(), text="how much is it", slots={"current_product_id": "11"})
    assert "1499 (was 1999)" in text and "4 left" in text and "Size: S, M, L, XL" in text


def test_product_details_by_name():
    text, _ = run(A.ActionProductDetails(), text="price of trail running shoes",
                  entities=[{"entity": "product", "value": "trail running shoes"}])
    assert "3999" in text and "In stock" in text


# ---- pages ------------------------------------------------------------------
def test_page_answer_shipping():
    text, _ = run(A.ActionAnswerFromSite(), text="is there free shipping?")
    assert "free on all orders above" in text and "/shipping-policy/" in text


def test_page_answer_contact():
    text, _ = run(A.ActionAnswerFromSite(), text="what is your phone number")
    assert "98765" in text


def test_page_answer_unknown():
    text, _ = run(A.ActionAnswerFromSite(), text="xyzzy quantum banana")
    assert text == "utter_default"


# ---- orders -----------------------------------------------------------------
def order(number, email, topic="status", attempts=0):
    return run(A.ActionOrderInfo(), slots={"order_number": number, "order_email": email,
                                           "order_topic": topic, "order_attempts": attempts})


def test_order_status_processing():
    text, slots = order("1001", "anita@example.com")
    assert "being prepared for shipping" in text and "packed and will ship" in text
    assert "Expected delivery date" in text and slots["order_verified"] is True


def test_order_wrong_email_hides_order():
    text, slots = order("1001", "someone@else.com")
    assert text == "utter_order_not_found" and slots["order_attempts"] == 1
    assert slots["order_number"] is None


def test_order_missing():
    text, _ = order("9999", "anita@example.com")
    assert text == "utter_order_not_found"


def test_order_too_many_attempts():
    text, _ = order("1001", "anita@example.com", attempts=3)
    assert text == "utter_too_many_attempts"


def test_delivery_tracking():
    text, _ = order("1002", "rahul@example.com", topic="delivery")
    assert "DLV123456" in text and "track.example" in text


def test_return_eligible():
    text, _ = order("1002", "rahul@example.com", topic="return")
    assert "eligible for return until" in text and "/returns-and-refunds/" in text


def test_return_refunded():
    text, _ = order("1003", "sam@example.org", topic="return")
    assert "Refund of ₹ 899.00" in text and "fully refunded" in text


def test_form_validation():
    v = A.ValidateOrderForm()
    d = CollectingDispatcher()
    assert v.validate_order_number("#1002", d, None, {}) == {"order_number": "1002"}
    assert v.validate_order_number("abc", d, None, {}) == {"order_number": None}
    assert v.validate_order_email("my email is Rahul@Example.com", d, None, {}) == {"order_email": "rahul@example.com"}
    assert v.validate_order_email("nope", d, None, {}) == {"order_email": None}


def test_page_answer_refund_timeline():
    text, _ = run(A.ActionAnswerFromSite(), text="how long does a refund take")
    assert "5-7 business days" in text


def test_validators_ignore_cleared_slots():
    v, d = A.ValidateOrderForm(), CollectingDispatcher()
    assert v.validate_order_number(None, d, None, {}) == {"order_number": None}
    assert v.validate_order_email(None, d, None, {}) == {"order_email": None}
    assert d.messages == []
