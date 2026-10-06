"""Central configuration, read from environment variables / a .env file."""
import os
from pathlib import Path

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    """Like os.getenv, but an empty value also means "use the default".
    (docker compose passes unset variables through as empty strings.)"""
    val = os.getenv(name)
    return default if val is None or val.strip() == "" else val.strip()


def _bool(name: str, default: bool) -> bool:
    val = _env(name)
    if not val:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- WooCommerce store -------------------------------------------------------
WC_URL = _env("WC_URL", "").rstrip("/")
WC_CONSUMER_KEY = _env("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = _env("WC_CONSUMER_SECRET", "")
WC_VERIFY_SSL = _bool("WC_VERIFY_SSL", True)

# Sitemap: leave empty to auto-detect (/sitemap_index.xml for Yoast/RankMath,
# /wp-sitemap.xml for WordPress core, /sitemap.xml as a last resort).
SITEMAP_URL = _env("SITEMAP_URL", "")

# Public shop domain, if customers shop on a different domain than WooCommerce runs on
# (headless / Next.js storefronts), e.g. https://jiaexpress.com. Used for product links
# and as a fallback page list when the sitemap can't be read.
STOREFRONT_URL = _env("STOREFRONT_URL", "").rstrip("/")
FALLBACK_PAGE_PATHS = _list(
    "FALLBACK_PAGE_PATHS",
    "/shipping-policy,/shipping,/refund-policy,/return-policy,/returns,/refund_returns,/contact,/contact-us,"
    "/about-us,/about,/faq,/faqs,/privacy-policy,/terms-of-service,/terms-and-conditions",
)

# --- Bridge behaviour ---------------------------------------------------------
CACHE_DIR = Path(_env("CACHE_DIR", str(PROJECT_ROOT / "bridge_cache")))
LOOKUP_FILE = Path(_env("LOOKUP_FILE", str(PROJECT_ROOT / "lookups" / "lookups.yml")))
CRAWL_MAX_PAGES = int(_env("CRAWL_MAX_PAGES", "300"))
CRAWL_KINDS = _list("CRAWL_KINDS", "page,post")
REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT", "15"))
# Browser-like, so CDN bot filters (e.g. Cloudflare) let the sync read your pages.
# Still contains "RasaWooBridge" so you can allow-list it explicitly.
USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 RasaWooBridge/1.1",
)

# Orders / returns
RETURN_WINDOW_DAYS = int(_env("RETURN_WINDOW_DAYS", "30"))
MAX_ORDER_VERIFY_ATTEMPTS = int(_env("MAX_ORDER_VERIFY_ATTEMPTS", "3"))
# Order meta keys used by popular delivery-date plugins. The first one found wins.
DELIVERY_DATE_META_KEYS = _list(
    "DELIVERY_DATE_META_KEYS",
    "_delivery_date,delivery_date,_orddd_timestamp,_orddd_lite_timestamp,"
    "jckwds_date,_jckwds_date,Delivery Date,_wcdd_delivery_date",
)

# Page answers: minimum BM25 score before we trust a page snippet.
PAGE_ANSWER_MIN_SCORE = float(_env("PAGE_ANSWER_MIN_SCORE", "1.5"))


def woo_api_configured() -> bool:
    return bool(WC_URL and WC_CONSUMER_KEY and WC_CONSUMER_SECRET)
