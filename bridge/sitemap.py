"""Fetch and classify a WordPress / WooCommerce sitemap.

Supports:
  * WordPress core sitemaps     (/wp-sitemap.xml)
  * Yoast SEO / Rank Math       (/sitemap_index.xml, product-sitemap.xml ...)
  * Any plain sitemap.xml / sitemap index, gzipped or not.
"""
import gzip
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Iterable, List, Optional
from urllib.parse import unquote, urlparse

from . import config
from .http import make_session

log = logging.getLogger(__name__)

PRODUCT = "product"
CATEGORY = "product_cat"
PAGE = "page"
POST = "post"
OTHER = "other"


@dataclass
class SitemapEntry:
    url: str
    kind: str
    lastmod: Optional[str] = None
    source: Optional[str] = None  # which child sitemap it came from

    @property
    def slug(self) -> str:
        return url_slug(self.url)

    def to_dict(self):
        return asdict(self)


def url_slug(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return unquote(parts[-1]) if parts else ""


def slug_to_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _classify_by_sitemap(sitemap_url: str) -> Optional[str]:
    name = urlparse(sitemap_url).path.rsplit("/", 1)[-1].lower()
    # Order matters: product_cat before product.
    if re.search(r"product[_-]cat", name):
        return CATEGORY
    if re.search(r"product[_-]tag|product[_-]brand|pa_", name):
        return OTHER
    if re.search(r"(^|[-_])product([-_]|s?\.xml|\d)", name):
        return PRODUCT
    if re.search(r"(^|[-_])page([-_]|s?\.xml|\d)", name):
        return PAGE
    if re.search(r"(^|[-_])post([-_]|s?\.xml|\d)", name):
        return POST
    return None


def _classify_by_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/product-category/" in path:
        return CATEGORY
    if "/product/" in path:
        return PRODUCT
    if re.search(r"/\d{4}/\d{2}/", path):
        return POST
    return PAGE


class SitemapReader:
    def __init__(self, session=None, max_depth: int = 4):
        self.session = session or make_session()
        self.max_depth = max_depth

    # -- discovery --------------------------------------------------------------
    def discover(self, base_url: str) -> str:
        """Return the first sitemap URL that responds with XML."""
        if config.SITEMAP_URL:
            return config.SITEMAP_URL
        candidates = [
            "/sitemap_index.xml",  # Yoast, Rank Math
            "/wp-sitemap.xml",     # WordPress core
            "/sitemap.xml",
        ]
        for path in candidates:
            url = base_url.rstrip("/") + path
            try:
                r = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
                if r.ok and b"<" in r.content[:200] and (b"urlset" in r.content or b"sitemapindex" in r.content):
                    log.info("Using sitemap %s", url)
                    return url
            except Exception as exc:  # noqa: BLE001
                log.debug("sitemap probe %s failed: %s", url, exc)
        raise RuntimeError(
            f"No sitemap found under {base_url}. Set SITEMAP_URL in .env explicitly."
        )

    # -- fetching ---------------------------------------------------------------
    def _fetch(self, url: str) -> bytes:
        r = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.content
        if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data

    def read(self, sitemap_url: str) -> List[SitemapEntry]:
        seen = set()
        entries: List[SitemapEntry] = []
        self._read(sitemap_url, 0, seen, entries)
        # de-duplicate by URL, keep first
        uniq, urls = [], set()
        for e in entries:
            if e.url not in urls:
                urls.add(e.url)
                uniq.append(e)
        log.info("Sitemap: %d URLs (%s)", len(uniq), summarize(uniq))
        return uniq

    def _read(self, url: str, depth: int, seen: set, out: List[SitemapEntry]):
        if url in seen or depth > self.max_depth:
            return
        seen.add(url)
        try:
            root = ET.fromstring(self._fetch(url))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read sitemap %s: %s", url, exc)
            return

        kind_hint = _classify_by_sitemap(url)
        root_tag = _local(root.tag)
        if root_tag == "sitemapindex":
            for sm in root:
                loc = _child_text(sm, "loc")
                if loc:
                    self._read(loc, depth + 1, seen, out)
        elif root_tag == "urlset":
            for u in root:
                loc = _child_text(u, "loc")
                if not loc:
                    continue
                kind = kind_hint or _classify_by_url(loc)
                out.append(SitemapEntry(loc, kind, _child_text(u, "lastmod"), url))


def _child_text(el, name: str) -> Optional[str]:
    for c in el:
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return None


def summarize(entries: Iterable[SitemapEntry]) -> str:
    counts = {}
    for e in entries:
        counts[e.kind] = counts.get(e.kind, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
