"""Tests for the Chatwoot connector with a fake Chatwoot API and a fake Rasa."""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CW_PORT, RASA_PORT, CONN_PORT = 8191, 8192, 8193
SECRET = "test-secret"
os.environ.update(
    CHATWOOT_URL=f"http://127.0.0.1:{CW_PORT}", CHATWOOT_BOT_TOKEN="bot-token",
    CHATWOOT_WEBHOOK_SECRET=SECRET, CHATWOOT_URL_TOKEN="", RASA_URL=f"http://127.0.0.1:{RASA_PORT}",
)
from bridge import chatwoot_connector as C  # noqa: E402

CW_CALLS, RASA_CALLS = [], []


class FakeChatwoot(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CW_CALLS.append({"path": self.path, "token": self.headers.get("api_access_token"), "body": body})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class FakeRasa(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RASA_CALLS.append(body)
        msg = body["message"].lower()
        if "human" in msg:
            out = [{"text": "Connecting you to our team."}, {"custom": {"handoff": True}}]
        else:
            out = [{"text": f"echo: {body['message']}"}, {"text": "second bubble"}]
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


for port, h in ((CW_PORT, FakeChatwoot), (RASA_PORT, FakeRasa)):
    srv = ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
_conn = C.make_server(CONN_PORT)
threading.Thread(target=_conn.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{CONN_PORT}/chatwoot/webhook"


def payload(mid, content, status="pending", mtype="incoming", private=False):
    return {"event": "message_created", "id": mid, "content": content, "message_type": mtype,
            "private": private, "account": {"id": 1}, "inbox": {"id": 3},
            "conversation": {"id": 42, "status": status}, "sender": {"type": "contact"}}


def post(p, secret=SECRET, ts=None):
    raw = json.dumps(p).encode()
    ts = str(int(ts if ts is not None else time.time()))
    sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return requests.post(URL, data=raw, headers={"Content-Type": "application/json",
                                                 "X-Chatwoot-Signature": sig, "X-Chatwoot-Timestamp": ts})


def wait_for(n, lst, timeout=5):
    end = time.time() + timeout
    while time.time() < end and len(lst) < n:
        time.sleep(0.05)
    return lst


def reset():
    CW_CALLS.clear()
    RASA_CALLS.clear()


def test_health():
    assert requests.get(f"http://127.0.0.1:{CONN_PORT}/health").status_code == 200


def test_reply_is_posted_to_conversation():
    reset()
    assert post(payload(1, "do you have hoodies?")).status_code == 200
    wait_for(2, CW_CALLS)
    assert RASA_CALLS[0]["sender"] == C.sender_for(1, 42) and RASA_CALLS[0]["message"] == "do you have hoodies?"
    assert RASA_CALLS[0]["sender"].startswith("cw-1-42-") and len(RASA_CALLS[0]["sender"]) > 20
    assert [c["path"] for c in CW_CALLS] == ["/api/v1/accounts/1/conversations/42/messages"] * 2
    assert CW_CALLS[0]["token"] == "bot-token"
    assert CW_CALLS[0]["body"] == {"content": "echo: do you have hoodies?", "message_type": "outgoing", "private": False}
    assert CW_CALLS[1]["body"]["content"] == "second bubble"


def test_handoff_toggles_status_open():
    reset()
    post(payload(2, "talk to a human"))
    wait_for(2, CW_CALLS)
    assert CW_CALLS[0]["body"]["content"] == "Connecting you to our team."
    assert CW_CALLS[1]["path"].endswith("/conversations/42/toggle_status")
    assert CW_CALLS[1]["body"] == {"status": "open"}


def test_ignores_non_customer_and_human_owned_messages():
    reset()
    post(payload(3, "bot's own reply", mtype="outgoing"))
    post(payload(4, "agent note", private=True))
    post(payload(5, "customer writes while agent handles it", status="open"))
    post({"event": "conversation_created", "id": 6})
    time.sleep(0.5)
    assert RASA_CALLS == [] and CW_CALLS == []


def test_integer_message_type_is_accepted():
    reset()
    post(payload(7, "hello", mtype=0))
    wait_for(2, CW_CALLS)
    assert RASA_CALLS and RASA_CALLS[0]["message"] == "hello"


def test_duplicate_delivery_processed_once():
    reset()
    post(payload(8, "hi again"))
    post(payload(8, "hi again"))
    time.sleep(0.6)
    assert len(RASA_CALLS) == 1


def test_bad_signature_rejected():
    reset()
    assert post(payload(9, "hi"), secret="wrong").status_code == 401
    assert post(payload(10, "hi"), ts=time.time() - 3600).status_code == 401  # replayed/old
    r = requests.post(URL, json=payload(11, "hi"))  # unsigned
    assert r.status_code == 401
    time.sleep(0.3)
    assert RASA_CALLS == []


def test_rasa_down_hands_off(monkeypatch=None):
    reset()
    old = C.RASA_URL
    C.RASA_URL = "http://127.0.0.1:1"  # nothing listens here
    try:
        post(payload(12, "hello?"))
        wait_for(2, CW_CALLS)
    finally:
        C.RASA_URL = old
    assert "trouble" in CW_CALLS[0]["body"]["content"]
    assert CW_CALLS[1]["body"] == {"status": "open"}
