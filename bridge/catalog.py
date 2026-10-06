"""Product / category catalogue built from the sitemap (+ WooCommerce API when available)."""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from rapidfuzz import fuzz, process

from . import config
from .sitemap import CATEGORY, PRODUCT, SitemapEntry, slug_to_title, url_slug

log = logging.getLogger(__name__)

# Words that carry no product meaning in shopping questions.
FILLER = set(
    """a an the do does you have any is are there show me find looking for look i im i'm want need
    buy get some sell selling price of how much cost costs what about please can could would like
    in stock available availability details detail info information tell more on this that it your
    store shop products product item items""".split()
)


def clean_query(text: str) -> str:
    words = re.findall(r"[\w\-']+", (text or "").lower())
    kept = [w for w in words if w not in FILLER]
    return " ".join(kept) or (text or "").strip()


class Catalog:
    def __init__(self, products: List[Dict] = None, categories: List[Dict] = None):
        self.products = products or []
        self.categories = categories or []
        self._index()

    def _index(self):
        self._names = [p["name"] for p in self.products]
        self._by_id = {p["id"]: p for p in self.products if p.get("id") is not None}
        self._by_slug = {p["slug"]: p for p in self.products}

    # -- building ---------------------------------------------------------------
    @classmethod
    def build(cls, entries: List[SitemapEntry], woo=None) -> "Catalog":
        sitemap_products = {e.slug: e for e in entries if e.kind == PRODUCT}
        sitemap_cats = {e.slug: e for e in entries if e.kind == CATEGORY}
        products, categories = [], []

        if woo is not None:
            api_products = woo.all_products()
            for p in api_products:
                products.append(
                    {
                        "id": p["id"],
                        "name": _strip_html(p["name"]),
                        "slug": p["slug"],
                        # Prefer the storefront URL from the sitemap (headless shops serve products
                        # on a different domain than WooCommerce's permalink).
                        "url": sitemap_products[p["slug"]].url if p["slug"] in sitemap_products else p.get("permalink", ""),
                        "categories": [c["name"] for c in p.get("categories", [])],
                        "price": p.get("price"),
                        "stock_status": p.get("stock_status"),
                        "sku": p.get("sku"),
                        "in_sitemap": p["slug"] in sitemap_products,
                    }
                )
            known = {p["slug"] for p in products}
            # Products present in the sitemap but not returned by the API (rare)
            for slug, e in sitemap_products.items():
                if slug not in known:
                    products.append(_from_sitemap(e))
            for c in woo.all_categories():
                e = sitemap_cats.get(c["slug"])
                categories.append(
                    {"id": c["id"], "name": _strip_html(c["name"]), "slug": c["slug"], "count": c.get("count"),
                     "url": e.url if e else ""}
                )
        else:
            products = [_from_sitemap(e) for e in sitemap_products.values()]
            categories = [
                {"id": None, "name": slug_to_title(e.slug), "slug": e.slug, "count": None, "url": e.url}
                for e in sitemap_cats.values()
            ]
        log.info("Catalog: %d products, %d categories", len(products), len(categories))
        return cls(products, categories)

    # -- persistence ------------------------------------------------------------
    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"products": self.products, "categories": self.categories}, indent=1))

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(data.get("products", []), data.get("categories", []))

    # -- querying ---------------------------------------------------------------
    def get(self, product_id=None, slug=None) -> Optional[Dict]:
        if product_id is not None:
            return self._by_id.get(int(product_id))
        if slug:
            return self._by_slug.get(slug)
        return None

    def public_url(self, product: dict) -> str:
        """Storefront URL for a product dict from the API (falls back to its permalink)."""
        hit = self._by_slug.get(product.get("slug")) or (self._by_id.get(product.get("id")) if product.get("id") else None)
        return (hit or {}).get("url") or product.get("permalink", "")

    def find_category(self, text: str, min_score: int = 80) -> Optional[Dict]:
        if not self.categories or not text:
            return None
        names = [c["name"] for c in self.categories]
        hit = process.extractOne(clean_query(text), names, scorer=fuzz.token_set_ratio)
        if hit and hit[1] >= min_score:
            return self.categories[hit[2]]
        return None

    def search(self, text: str, limit: int = 5, min_score: int = 60) -> List[Dict]:
        q = clean_query(text)
        if not q or not self.products:
            return []
        # Score on name, and give a smaller weight to category matches.
        scored = []
        for i, p in enumerate(self.products):
            name_score = fuzz.WRatio(q, p["name"].lower())
            cat_score = max((fuzz.token_set_ratio(q, c.lower()) for c in p.get("categories", [])), default=0)
            score = max(name_score, cat_score * 0.85)
            if score >= min_score:
                scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        return [self.products[i] for _, i in scored[:limit]]

    def lookup_names(self) -> Dict[str, List[str]]:
        prods = sorted({_lookup_clean(p["name"]) for p in self.products} - {""})
        cats = sorted({_lookup_clean(c["name"]) for c in self.categories} - {"", "uncategorized"})
        return {"product": prods, "product_category": cats}


def _from_sitemap(e: SitemapEntry) -> Dict:
    return {
        "id": None, "name": slug_to_title(e.slug), "slug": e.slug, "url": e.url,
        "categories": [], "price": None, "stock_status": None, "sku": None, "in_sitemap": True,
    }


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&amp;", "&").replace("&#8211;", "-").strip()


def _lookup_clean(name: str) -> str:
    # Lookup tables are regexes built from the text; keep them simple and lower-case.
    name = re.sub(r"\s+", " ", name.lower()).strip()
    return name if 2 <= len(name) <= 80 else ""
