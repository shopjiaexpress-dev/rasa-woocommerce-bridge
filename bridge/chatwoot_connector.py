"""Chatwoot Agent Bot <-> Rasa connector.

Chatwoot sends every new message in a bot-enabled inbox to this webhook. We
forward customer messages to Rasa's REST channel and post Rasa's replies back to
the conversation with the Chatwoot API (using the Agent Bot's access token).

Human handoff: when Rasa returns a custom payload {"handoff": true}, the
conversation is switched from "pending" (bot) to "open" (human agents). While a
conversation is open, the bot stays silent. Agents can set it back to "pending"
to give it back to the bot.

Run:  python -m bridge.chatwoot_connector      (listens on :5056)
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger("chatwoot")


def _env(name, default=""):
    v = os.getenv(name)
    return default if v is None or not v.strip() else v.strip()


CHATWOOT_URL = _env("CHATWOOT_URL").rstrip("/")
CHATWOOT_BOT_TOKEN = _env("CHATWOOT_BOT_TOKEN")
# HMAC secret shown on the Agent Bot page (newer Chatwoot versions). Optional.
CHATWOOT_WEBHOOK_SECRET = _env("CHATWOOT_WEBHOOK_SECRET")
# Alternative for older Chatwoot versions: add ?token=<value> to the webhook URL.
CHATWOOT_URL_TOKEN = _env("CHATWOOT_URL_TOKEN")
RASA_URL = _env("RASA_URL", "http://rasa:5005").rstrip("/")
PORT = int(_env("CHATWOOT_CONNECTOR_PORT", "5056"))
MAX_SIGNATURE_AGE = 300  # seconds
TIMEOUT = 30


class _LRU:
    """Remembers recently processed message ids (Chatwoot may retry deliveries)."""

    def __init__(self, size=2000):
        self.size, self.d, self.lock = size, OrderedDict(), threading.Lock()

    def seen(self, key) -> bool:
        with self.lock:
            if key in self.d:
                return True
            self.d[key] = None
            if len(self.d) > self.size:
                self.d.popitem(last=False)
            return False


_seen = _LRU()
_conv_locks: dict = {}
_conv_locks_guard = threading.Lock()


def _conv_lock(key) -> threading.Lock:
    with _conv_locks_guard:
        return _conv_locks.setdefault(key, threading.Lock())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify(headers, raw: bytes, query: dict) -> bool:
    if CHATWOOT_URL_TOKEN:
        if not hmac.compare_digest(query.get("token", [""])[0], CHATWOOT_URL_TOKEN):
            return False
    if CHATWOOT_WEBHOOK_SECRET:
        sig = headers.get("X-Chatwoot-Signature", "")
        ts = headers.get("X-Chatwoot-Timestamp", "")
        if not sig or not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SIGNATURE_AGE:
            return False
        expected = "sha256=" + hmac.new(
            CHATWOOT_WEBHOOK_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
    return True


# ---------------------------------------------------------------------------
# Chatwoot API
# ---------------------------------------------------------------------------
def _cw(method, path, **kw):
    r = requests.request(
        method, f"{CHATWOOT_URL}/api/v1/{path.lstrip('/')}",
        headers={"api_access_token": CHATWOOT_BOT_TOKEN, "Content-Type": "application/json"},
        timeout=TIMEOUT, **kw,
    )
    if r.status_code >= 400:
        log.error("Chatwoot %s %s -> %s %s", method, path, r.status_code, r.text[:200])
    return r


def send_message(account_id, conversation_id, text):
    return _cw("POST", f"accounts/{account_id}/conversations/{conversation_id}/messages",
               json={"content": text, "message_type": "outgoing", "private": False})


def handoff(account_id, conversation_id):
    log.info("handoff conversation %s to human agents", conversation_id)
    return _cw("POST", f"accounts/{account_id}/conversations/{conversation_id}/toggle_status",
               json={"status": "open"})


# ---------------------------------------------------------------------------
# Rasa
# ---------------------------------------------------------------------------
def ask_rasa(sender_id: str, text: str, metadata: dict) -> list:
    r = requests.post(f"{RASA_URL}/webhooks/rest/webhook",
                      json={"sender": sender_id, "message": text, "metadata": metadata}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() or []


def render(msg: dict) -> str:
    parts = []
    if msg.get("text"):
        parts.append(msg["text"])
    if msg.get("image"):
        parts.append(msg["image"])
    for b in msg.get("buttons") or []:
        parts.append(f"• {b.get('title')}")
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------
def should_handle(p: dict) -> bool:
    if p.get("event") != "message_created":
        return False
    if p.get("message_type") not in ("incoming", 0):   # ignore our own/agent messages
        return False
    if p.get("private"):
        return False
    conv = p.get("conversation") or {}
    if conv.get("status") not in (None, "pending"):    # a human has taken over
        return False
    return bool((p.get("content") or "").strip())


def process(p: dict):
    account_id = (p.get("account") or {}).get("id") or (p.get("conversation") or {}).get("account_id")
    conversation_id = (p.get("conversation") or {}).get("id")
    if not account_id or not conversation_id:
        log.warning("payload without account/conversation id")
        return
    sender_id = f"cw-{account_id}-{conversation_id}"
    with _conv_lock(sender_id):  # keep replies in order per conversation
        try:
            replies = ask_rasa(sender_id, p["content"].strip(), {
                "chatwoot": {"account_id": account_id, "conversation_id": conversation_id,
                             "inbox_id": (p.get("inbox") or {}).get("id")}})
        except Exception as exc:  # noqa: BLE001
            log.error("Rasa unavailable: %s", exc)
            send_message(account_id, conversation_id,
                         "Sorry, I'm having trouble right now. Connecting you to our team.")
            handoff(account_id, conversation_id)
            return
        wants_handoff = False
        for m in replies:
            if isinstance(m.get("custom"), dict) and m["custom"].get("handoff"):
                wants_handoff = True
                continue
            text = render(m)
            if text:
                send_message(account_id, conversation_id, text)
        if wants_handoff:
            handoff(account_id, conversation_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "RasaChatwoot/1.0"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _reply(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path in ("/health", "/chatwoot/health"):
            ok = bool(CHATWOOT_URL and CHATWOOT_BOT_TOKEN)
            return self._reply(200 if ok else 503, b"ok" if ok else b"CHATWOOT_URL / CHATWOOT_BOT_TOKEN not set")
        self._reply(404, b"not found")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/webhook", "/chatwoot/webhook"):
            return self._reply(404, b"not found")
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if not verify(self.headers, raw, parse_qs(u.query)):
            log.warning("rejected webhook with bad signature/token from %s", self.client_address[0])
            return self._reply(401, b"unauthorized")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._reply(400, b"bad json")
        # Answer Chatwoot immediately; do the slow work in the background.
        self._reply(200)
        if should_handle(payload) and not _seen.seen(payload.get("id")):
            threading.Thread(target=process, args=(payload,), daemon=True).start()


def make_server(port=None):
    return ThreadingHTTPServer(("0.0.0.0", port or PORT), Handler)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (CHATWOOT_URL and CHATWOOT_BOT_TOKEN):
        log.warning("CHATWOOT_URL / CHATWOOT_BOT_TOKEN not set: replies can't be sent until they are.")
    if not (CHATWOOT_WEBHOOK_SECRET or CHATWOOT_URL_TOKEN):
        log.warning("No CHATWOOT_WEBHOOK_SECRET or CHATWOOT_URL_TOKEN: webhook is unauthenticated.")
    log.info("Chatwoot connector listening on :%s, Rasa at %s", PORT, RASA_URL)
    make_server().serve_forever()


if __name__ == "__main__":
    main()
