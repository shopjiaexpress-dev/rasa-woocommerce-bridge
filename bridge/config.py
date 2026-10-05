"""Central configuration, read from environment variables / a .env file."""
import os
from pathlib import Path

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- WooCommerce store -------------------------------------------------------
WC_URL = os.getenv("WC_URL", "").rstrip("/")
WC_CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY", "")
WC_CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET", "")
WC_VERIFY_SSL = _bool("WC_VERIFY_SSL", True)

# Sitemap: leave empty to auto-detect (/sitemap_index.xml for Yoast/RankMath,
# /wp-sitemap.xml for WordPress core, /sitemap.xml as a last resort).
SITEMAP_URL = os.getenv("SITEMAP_URL", "")

# --- Bridge behaviour ---------------------------------------------------------
CACHE_DIR = Path(os.getenv("CACHE_DIR", str(PROJECT_ROOT / "bridge_cache")))
LOOKUP_FILE = Path(os.getenv("LOOKUP_FILE", str(PROJECT_ROOT / "data" / "lookups.yml")))
CRAWL_MAX_PAGES = int(os.getenv("CRAWL_MAX_PAGES", "300"))
CRAWL_KINDS = _list("CRAWL_KINDS", "page,post")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))
USER_AGENT = os.getenv("USER_AGENT", "RasaWooBridge/1.0 (+chatbot sync)")

# Orders / returns
RETURN_WINDOW_DAYS = int(os.getenv("RETURN_WINDOW_DAYS", "30"))
MAX_ORDER_VERIFY_ATTEMPTS = int(os.getenv("MAX_ORDER_VERIFY_ATTEMPTS", "3"))
# Order meta keys used by popular delivery-date plugins. The first one found wins.
DELIVERY_DATE_META_KEYS = _list(
    "DELIVERY_DATE_META_KEYS",
    "_delivery_date,delivery_date,_orddd_timestamp,_orddd_lite_timestamp,"
    "jckwds_date,_jckwds_date,Delivery Date,_wcdd_delivery_date",
)

# Page answers: minimum BM25 score before we trust a page snippet.
PAGE_ANSWER_MIN_SCORE = float(os.getenv("PAGE_ANSWER_MIN_SCORE", "2.0"))


def woo_api_configured() -> bool:
    return bool(WC_URL and WC_CONSUMER_KEY and WC_CONSUMER_SECRET)
