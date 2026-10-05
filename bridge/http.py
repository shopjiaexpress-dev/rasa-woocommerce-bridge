"""Shared HTTP session with retries."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers["User-Agent"] = config.USER_AGENT
    s.verify = config.WC_VERIFY_SSL
    return s
