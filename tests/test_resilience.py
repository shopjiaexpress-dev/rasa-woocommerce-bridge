"""The sync must save whatever it can: one failing stage must not leave the bot empty."""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_woo_server  # noqa: E402
from bridge import config, store, sync  # noqa: E402

PORT = 8089
BASE = f"http://127.0.0.1:{PORT}"
_srv = mock_woo_server.serve(PORT)
threading.Thread(target=_srv.serve_forever, daemon=True).start()


def setup(monkeypatch, **kw):
    tmp = Path(tempfile.mkdtemp())
    vals = dict(WC_URL=BASE, WC_CONSUMER_KEY="ck_test", WC_CONSUMER_SECRET="cs_test", SITEMAP_URL="",
                STOREFRONT_URL="", CACHE_DIR=tmp, LOOKUP_FILE=tmp / "lookups.yml")
    vals.update(kw)
    for k, v in vals.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(store, "_catalog", store._Cached(tmp / store.CATALOG_FILE, store.Catalog.load))
    monkeypatch.setattr(store, "_pages", store._Cached(tmp / store.PAGES_FILE, store.PageIndex.load))
    return tmp


def test_unreadable_sitemap_still_syncs_products_and_pages(monkeypatch):
    tmp = setup(monkeypatch, SITEMAP_URL=f"{BASE}/does-not-exist.xml", STOREFRONT_URL=BASE)
    report = sync.run()
    assert report["errors"] and "sitemap" in report["errors"]
    cat = json.loads((tmp / "catalog.json").read_text())
    assert len(cat["products"]) == 4
    # links use the storefront, not WooCommerce's permalink
    assert cat["products"][0]["url"].startswith(f"{BASE}/product/")
    assert report["page_chunks"] and report["page_chunks"] >= 3          # fallback page paths were crawled
    assert any("free on all orders" in c["text"] for c in json.loads((tmp / "pages.json").read_text()))


def test_api_failure_keeps_sitemap_products_and_pages(monkeypatch):
    tmp = setup(monkeypatch, WC_CONSUMER_SECRET="wrong")
    report = sync.run()
    assert "woocommerce_api" in report["errors"]
    assert report["products"] == 4                                        # from the sitemap
    assert report["page_chunks"] >= 3


def test_empty_result_does_not_overwrite_previous_cache(monkeypatch):
    tmp = setup(monkeypatch)
    sync.run()
    before = (tmp / "pages.json").read_text()
    monkeypatch.setattr(config, "CRAWL_KINDS", ["nothing"])              # crawl finds no pages
    sync.run()
    assert (tmp / "pages.json").read_text() == before


def test_product_details_falls_back_to_live_api(monkeypatch):
    setup(monkeypatch)  # empty cache: nothing synced yet
    from rasa_sdk import Tracker
    from rasa_sdk.executor import CollectingDispatcher
    from actions import actions as A
    monkeypatch.setattr(A, "_woo", None)
    t = Tracker("t", {"current_product_id": "11"}, {"text": "how much is it?", "intent": {}, "entities": []},
                [], False, None, {}, None)
    d = CollectingDispatcher()
    A.ActionProductDetails().run(d, t, {})
    assert "1499 (was 1999)" in d.messages[0]["text"]
