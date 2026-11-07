#!/usr/bin/env python3
"""Lead capture for the delivery-automation landing pages.

Listens on 127.0.0.1 only; nginx proxies POST /api/lead from software.clubemkt.digital.
Stores the submitted answers and nothing that identifies the machine that sent them.
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("LEAD_DB", "/var/lib/clubemkt-leads/leads.db")
LISTEN = (os.environ.get("LEAD_HOST", "127.0.0.1"), int(os.environ.get("LEAD_PORT", "8410")))
MAX_BODY_BYTES = 8192
DUPLICATE_TTL_SECONDS = 600
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 15
LEAD_PATH = "/api/lead"

FIELDS = {
    "business": 120,
    "contact": 120,
    "phone": 24,
    "email": 160,
    "volume": 40,
    "pain": 80,
    "notes": 800,
    "lang": 8,
    "source": 80,
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
CHANNEL_LIMIT = 8
CHANNEL_LENGTH = 40

_lock = threading.Lock()
_conn = None
_hits = {}


def connect():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 received_at TEXT NOT NULL,
                 business TEXT NOT NULL,
                 contact TEXT NOT NULL,
                 phone TEXT NOT NULL,
                 phone_digits TEXT NOT NULL,
                 email TEXT,
                 volume TEXT NOT NULL,
                 pain TEXT NOT NULL,
                 channels TEXT,
                 notes TEXT,
                 lang TEXT,
                 source TEXT
               )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS leads_phone ON leads (phone_digits, id)")
        _conn.commit()
    return _conn


def client_key(handler):
    """Bucket key for the transient limiter. nginx proxies, so the socket peer is always
    the box itself; the forwarded header carries the visitor. Used in memory only."""
    forwarded = handler.headers.get("x-forwarded-for") or ""
    first = forwarded.split(",")[0].strip()
    return first or handler.client_address[0]


def clamp(value, maximum):
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:maximum]


def digits(value):
    return re.sub(r"\D", "", value or "")


def validate(body):
    errors = []
    lead = {name: clamp(body.get(name), length) for name, length in FIELDS.items()}

    if len(lead["business"]) < 2:
        errors.append("business")
    if len(lead["contact"]) < 2:
        errors.append("contact")

    d = digits(lead["phone"])
    if len(d) < 7 or len(d) > 15:
        errors.append("phone")
    lead["phone_digits"] = d

    if lead["email"] and not EMAIL_RE.match(lead["email"]):
        errors.append("email")
    if not lead["volume"]:
        errors.append("volume")
    if not lead["pain"]:
        errors.append("pain")

    raw_channels = body.get("channels")
    channels = []
    if isinstance(raw_channels, list):
        channels = [clamp(c, CHANNEL_LENGTH) for c in raw_channels[:CHANNEL_LIMIT]]
        channels = [c for c in channels if c]
    lead["channels"] = json.dumps(channels, ensure_ascii=False)

    return errors, lead


def flood_blocked(peer):
    now = time.time()
    with _lock:
        kept = [t for t in _hits.get(peer, []) if now - t < RATE_LIMIT_WINDOW]
        if len(kept) >= RATE_LIMIT_MAX:
            _hits[peer] = kept
            return True
        kept.append(now)
        _hits[peer] = kept
        return False


def store(lead):
    conn = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_TTL_SECONDS)
              ).isoformat(timespec="seconds")
    with _lock:
        recent = conn.execute(
            "SELECT id FROM leads WHERE phone_digits = ? AND id > "
            "(SELECT COALESCE(MAX(id), 0) - 200 FROM leads) "
            "AND received_at > ?",
            (lead["phone_digits"], cutoff),
        ).fetchone()
        if recent:
            return None, "already_registered"
        cur = conn.execute(
            "INSERT INTO leads (received_at, business, contact, phone, phone_digits, email,"
            " volume, pain, channels, notes, lang, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             lead["business"], lead["contact"], lead["phone"], lead["phone_digits"],
             lead["email"], lead["volume"], lead["pain"], lead["channels"],
             lead["notes"], lead["lang"], lead["source"]),
        )
        conn.commit()
        return cur.lastrowid, None


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadCapture/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def respond(self, payload, status):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.respond({"ok": False, "error": "method_not_allowed"}, 405)

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("allow", "POST")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.split("?")[0] != LEAD_PATH:
            self.respond({"ok": False, "error": "not_found"}, 404)
            return

        peer = client_key(self)
        if flood_blocked(peer):
            self.respond({"ok": False, "error": "too_many_requests"}, 429)
            return

        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = MAX_BODY_BYTES + 1
        if length > MAX_BODY_BYTES:
            self.respond({"ok": False, "error": "too_large"}, 413)
            return

        try:
            body = json.loads(self.rfile.read(min(length, MAX_BODY_BYTES)) or b"")
        except (ValueError, UnicodeDecodeError):
            self.respond({"ok": False, "error": "bad_json"}, 400)
            return
        if not isinstance(body, dict):
            self.respond({"ok": False, "error": "bad_json"}, 400)
            return

        errors, lead = validate(body)
        if errors:
            self.respond({"ok": False, "error": "invalid_fields", "fields": errors}, 422)
            return

        try:
            lead_id, failure = store(lead)
        except sqlite3.Error:
            self.respond({"ok": False, "error": "storage_unavailable"}, 500)
            return
        if failure:
            self.respond({"ok": False, "error": failure}, 409)
            return
        self.respond({"ok": True, "id": lead_id}, 200)


if __name__ == "__main__":
    connect()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
