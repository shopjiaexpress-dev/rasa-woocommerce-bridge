"""Page answers on real-world markup (headless Next.js storefront behind Cloudflare:
bold-paragraph section titles, text in <div>/<a>, obfuscated e-mails)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import config  # noqa: E402
from bridge.catalog import Catalog  # noqa: E402
from bridge.page_index import PageIndex, extract_chunks  # noqa: E402
from bridge.sitemap import PRODUCT, SitemapEntry  # noqa: E402

F = Path(__file__).parent / "fixtures" / "jiaexpress"
PAGES = {"shipping": "/shipping-policy", "refund": "/refund-policy", "contact": "/contact", "about": "/about-us"}
INDEX = PageIndex([c for f, u in PAGES.items()
                   for c in extract_chunks((F / f"{f}.html").read_text(), "https://jiaexpress.com" + u)])


def best(q):
    score, chunk = INDEX.search(q, 1)[0]
    assert score >= config.PAGE_ANSWER_MIN_SCORE, (q, score)
    return chunk


def test_bold_paragraphs_become_sections():
    heads = [c["heading"] for c in INDEX.chunks if c["url"].endswith("/shipping-policy")]
    assert "3. Shipping Rates" in heads and "2. Transit & Delivery Time" in heads


def test_answers_come_from_the_right_section():
    assert best("is there free shipping?")["heading"] == "3. Shipping Rates"
    assert best("how long does delivery take?")["heading"] == "2. Transit & Delivery Time"
    assert best("how long does a refund take")["heading"] == "5. Refund Processing"
    assert best("can I cancel my order")["heading"] == "6. Order Cancellations"
    assert best("what is your return policy?")["url"].endswith("/refund-policy")


def test_contact_details_in_links_and_cloudflare_emails():
    assert "+91-9629942288" in best("what is your phone number")["text"]
    assert "support@jiaexpress.com" in best("how can I contact you?")["text"]
    assert "[email" not in " ".join(c["text"] for c in INDEX.chunks if c["url"].endswith("/contact"))


def test_product_links_use_storefront_url_from_sitemap():
    entries = [SitemapEntry("https://jiaexpress.com/product/axonit-a-82-7", PRODUCT)]

    class FakeWoo:
        def all_products(self):
            return [{"id": 7, "name": "AXONIT Saree", "slug": "axonit-a-82-7",
                     "permalink": "https://web.jiaexpress.com/product/axonit-a-82-7/", "categories": []}]

        def all_categories(self):
            return []

    cat = Catalog.build(entries, FakeWoo())
    assert cat.products[0]["url"] == "https://jiaexpress.com/product/axonit-a-82-7"
    assert cat.public_url({"slug": "axonit-a-82-7", "permalink": "https://web.jiaexpress.com/x/"}) == \
        "https://jiaexpress.com/product/axonit-a-82-7"
