"""Crawl the pages listed in the sitemap (shipping, returns, FAQ, blog...) and
search them with BM25 so the bot can answer from your own site content."""
import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup, Comment

from . import config
from .http import make_session
from .sitemap import SitemapEntry

log = logging.getLogger(__name__)

STOP = set(
    """a an and are as at be by can do does for from has have how i if in is it its me my of on or our
    so than that the their there this to was we what when where which who why will with you your
    please tell about get any""".split()
)
CHUNK_WORDS = 90
# Words that say nothing about the topic on their own (still used for synonym expansion).
GENERIC = {"long", "take", "much", "many", "know", "want", "need", "like", "would", "could", "there"}

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


def _is_pseudo_heading(el) -> bool:
    """<p><strong>3. Shipping Rates</strong></p>: a short paragraph that is entirely
    bold. Many themes and headless storefronts use these instead of <h2>/<h3>."""
    if el.name != "p":
        return False
    txt = el.get_text(" ", strip=True)
    if not txt or len(txt.split()) > 10 or txt.endswith((".", ",", ";")):
        return False
    bold = " ".join(b.get_text(" ", strip=True) for b in el.find_all(["strong", "b"]))
    return bold.strip() == txt


BLOCK_TAGS = {"p", "li", "div", "td", "th", "dd", "dt", "section", "article", "blockquote", "main",
              "h1", "h2", "h3", "h4", "h5", "h6", "address", "figcaption", "ul", "ol", "table", "tr"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _cf_decode(hexstr: str) -> str:
    """Decode a Cloudflare-obfuscated e-mail address (data-cfemail)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def extract_chunks(html: str, url: str) -> List[Dict]:
    """Split a page into (heading, text) sections.

    Text is collected from *every* block element (not only <p>/<li>), so content
    in <div>s or links, like a phone number in <a href="tel:">, isn't lost.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else url).split(" | ")[0].split(" – ")[0]
    for el in soup.select("[data-cfemail]"):  # Cloudflare e-mail protection
        el.replace_with(_cf_decode(el["data-cfemail"]))
    for bad in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg",
                     "iframe", "button", "select", "template"]):
        bad.decompose()
    main = (
        soup.select_one(".entry-content")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.select_one("#content")
        or soup.body
        or soup
    )

    # 1) Group visible strings by their nearest block ancestor, in document order.
    blocks = []
    for st in main.find_all(string=True):
        if isinstance(st, Comment):
            continue
        t = " ".join(st.split())
        if not t:
            continue
        blk = st.find_parent(lambda tag: tag.name in BLOCK_TAGS) or main
        if blocks and blocks[-1][0] is blk:
            blocks[-1][1].append(t)
        else:
            blocks.append((blk, [t]))

    # 2) Turn blocks into sections.
    chunks, heading, buf = [], title, []

    def flush():
        text = " ".join(buf).strip()
        if len(text.split()) >= 4:
            chunks.append({"url": url, "title": title, "heading": heading, "text": text})

    for blk, parts in blocks:
        txt = " ".join(parts).strip()
        if blk.name in HEADING_TAGS or _is_pseudo_heading(blk):
            flush()
            heading, buf = txt.rstrip(":"), []
            continue
        buf.append(txt)
        if sum(len(b.split()) for b in buf) >= CHUNK_WORDS:
            flush()
            buf = []
    flush()
    return chunks


def _slug_words(url: str) -> str:
    return re.sub(r"[-_/]+", " ", re.sub(r"^https?://[^/]+", "", url or ""))


class PageIndex:
    def __init__(self, chunks: List[Dict] = None):
        self.chunks = chunks or []
        self._build()

    def _build(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        # Heading and title words count twice: they describe what the chunk is about.
        self._docs = [
            tokenize(c["text"] + " " + (c["heading"] + " " + c["title"] + " " + _slug_words(c["url"])) * 2)
            for c in self.chunks
        ]
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
        chunks, failed = [], Counter()
        for i, e in enumerate(targets, 1):
            try:
                r = session.get(e.url, timeout=config.REQUEST_TIMEOUT)
                if r.ok and "html" in r.headers.get("Content-Type", "html"):
                    got = extract_chunks(r.text, e.url)
                    chunks.extend(got)
                    if not got:
                        failed["no text"] += 1
                else:
                    failed[f"HTTP {r.status_code}"] += 1
                    if r.status_code in (403, 429, 503) and "cloudflare" in r.headers.get("Server", "").lower():
                        failed["cloudflare"] += 1
            except Exception as exc:  # noqa: BLE001
                failed[type(exc).__name__] += 1
                log.warning("crawl %s failed: %s", e.url, exc)
            if i % 25 == 0:
                log.info("crawled %d/%d pages", i, len(targets))
        if failed:
            log.warning("Pages that could not be read: %s", dict(failed))
            if failed.get("cloudflare"):
                log.warning("Cloudflare is blocking the crawler. In Cloudflare > Security > WAF, add a custom rule "
                            "that skips bot checks when the User-Agent contains 'RasaWooBridge'.")
        log.info("Page index: %d chunks from %d pages", len(chunks), len(targets))
        index = cls(chunks)
        index.stats = {"pages": len(targets), "chunks": len(chunks), "failed": dict(failed)}
        return index

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.chunks, indent=1))

    @classmethod
    def load(cls, path: Path) -> "PageIndex":
        return cls(json.loads(path.read_text())) if path.exists() else cls()

    def search(self, query: str, k: int = 3) -> List[Tuple[float, Dict]]:
        raw = [stem(w) for w in re.findall(r"[a-z0-9]+", (query or "").lower())]
        weights = {}
        for t in raw:
            if t not in STOP and t not in GENERIC and len(t) > 1:
                weights[t] = 1.0
        for t in raw:  # expansions also come from generic words ("how long" -> days)
            for x in QUERY_SYNONYMS.get(t, []):
                x = stem(x)
                weights.setdefault(x, 0.35)
        if not weights or not self.chunks:
            return []
        scores = []
        for i, tf in enumerate(self._tf):
            dl = len(self._docs[i])
            s_ = 0.0
            for t, w in weights.items():
                f = tf.get(t)
                if f:
                    s_ += w * self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1)))
            if s_ > 0:
                scores.append((s_, i))
        scores.sort(key=lambda x: -x[0])
        return [(round(sc, 3), self.chunks[i]) for sc, i in scores[:k]]
