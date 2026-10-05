"""Runtime access to the synced cache. Reloads automatically when `sync` rewrites
the files, so the action server never needs a restart after a sync."""
import logging
import threading
from pathlib import Path

from . import config
from .catalog import Catalog
from .page_index import PageIndex

log = logging.getLogger(__name__)

CATALOG_FILE = "catalog.json"
PAGES_FILE = "pages.json"


class _Cached:
    def __init__(self, path: Path, loader):
        self.path, self.loader = path, loader
        self._mtime, self._obj = None, None
        self._lock = threading.Lock()

    def get(self):
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if self._obj is None or mtime != self._mtime:
            with self._lock:
                self._obj = self.loader(self.path)
                self._mtime = mtime
                log.info("Loaded %s", self.path)
        return self._obj


_catalog = _Cached(config.CACHE_DIR / CATALOG_FILE, Catalog.load)
_pages = _Cached(config.CACHE_DIR / PAGES_FILE, PageIndex.load)


def catalog() -> Catalog:
    return _catalog.get()


def pages() -> PageIndex:
    return _pages.get()
