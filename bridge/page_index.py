"""Crawl the pages listed in the sitemap (shipping, returns, FAQ, blog...) and
search them with BM25 so the bot can answer from your own site content."""
import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from . import config
from .http import make_session
from .sitemap import SitemapEntry

log = logging.getLogger(__name__)

STOP = set(
    """a an and are as at be by can do does for from has have how i if in is it its me my of on or our
    so than that the their there this to was we what when where which who why will with you your
    please tell about get any""".split()
)
CHUNK_WORDS = 120

# Customers and store pages often use different words for the same thing.
# Query words are expanded with these before searching. Extend freely.
QUERY_SYNONYMS = {
    "phone": ["call", "contact", "number"], "number": ["call", "contact"], "email": ["contact", "mail"],
    "contact": ["call", "email", "support", "reach"], "reach": ["contact", "call", "email"],
    "delivery": ["shipping", "ship", "deliver"], "deliver": ["shipping", "delivery"], "shipping": ["delivery", "ship"],
    "ship": ["shipping", "delivery"], "dispatch": ["ship", "shipping"],
    "return": ["returns", "refund", "exchange"], "refund": ["refunds", "return"], "exchange": ["return", "returns"],
    "money": ["refund"], "cost": ["charge", "charges", "fee", "price"], "free": ["charge", "charges"],
    "long": ["days", "time", "timeline"], "time": ["days", "timeline"], "take": ["days"],
    "hours": ["timing", "open", "monday", "saturday"], "open": ["hours", "timing"],
    "pay": ["payment", "cod"], "payment": ["pay", "cod", "upi", "card"], "cod": ["cash", "delivery", "payment"],
    "cancel": ["cancellation", "cancelled"], "located": ["address", "location"], "address": ["location", "located"],
}


def stem(w: str) -> str:
    """Very light English stemmer: refunds/refunded/refunding -> refund."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def tokenize(text: str) -> List[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


def extract_chunks(html: str, url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else url).split(" | ")[0].split(" – ")[0]
    for bad in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        bad.decompose()
    main = (
        soup.select_one(".entry-content")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.select_one("#content")
        or soup.body
        or soup
    )
    chunks, heading, buf = [], title, []

    def flush():
        text = " ".join(buf).strip()
        if len(text.split()) >= 8:
            chunks.append({"url": url, "title": title, "heading": heading, "text": text})

    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "dt", "dd"]):
        if el.find(["p", "li"]) is not None and el.name in ("li", "td", "dd"):
            continue  # avoid double-counting nested blocks
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            flush()
            heading, buf = txt, []
            continue
        buf.append(txt)
        if sum(len(b.split()) for b in buf) >= CHUNK_WORDS:
            flush()
            buf = []
    flush()
    return chunks


class PageIndex:
    def __init__(self, chunks: List[Dict] = None):
        self.chunks = chunks or []
        self._build()

    def _build(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        # Heading and title words count twice: they describe what the chunk is about.
        self._docs = [tokenize(c["text"] + " " + (c["heading"] + " " + c["title"]) * 2) for c in self.chunks]
        self._tf = [Counter(d) for d in self._docs]
        n = len(self._docs) or 1
        self._avgdl = sum(len(d) for d in self._docs) / n if self._docs else 0
        df = Counter()
        for d in self._docs:
            df.update(set(d))
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def crawl(cls, entries: List[SitemapEntry], kinds=None, max_pages: int = None, session=None) -> "PageIndex":
        kinds = set(kinds or config.CRAWL_KINDS)
        max_pages = max_pages or config.CRAWL_MAX_PAGES
        session = session or make_session()
        targets = [e for e in entries if e.kind in kinds][:max_pages]
        chunks = []
        for i, e in enumerate(targets, 1):
            try:
                r = session.get(e.url, timeout=config.REQUEST_TIMEOUT)
                if r.ok and "html" in r.headers.get("Content-Type", "html"):
                    chunks.extend(extract_chunks(r.text, e.url))
            except Exception as exc:  # noqa: BLE001
                log.warning("crawl %s failed: %s", e.url, exc)
            if i % 25 == 0:
                log.info("crawled %d/%d pages", i, len(targets))
        log.info("Page index: %d chunks from %d pages", len(chunks), len(targets))
        return cls(chunks)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.chunks, indent=1))

    @classmethod
    def load(cls, path: Path) -> "PageIndex":
        return cls(json.loads(path.read_text())) if path.exists() else cls()

    def search(self, query: str, k: int = 3) -> List[Tuple[float, Dict]]:
        base = tokenize(query)
        if not base or not self.chunks:
            return []
        q = list(base)
        for t in base:
            q.extend(stem(x) for x in QUERY_SYNONYMS.get(t, []) if stem(x) not in q)
        scores = []
        for i, tf in enumerate(self._tf):
            dl = len(self._docs[i])
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1)))
            if s > 0:
                scores.append((s, i))
        scores.sort(key=lambda x: -x[0])
        return [(round(s, 3), self.chunks[i]) for s, i in scores[:k]]
