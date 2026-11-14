#!/usr/bin/env python3
"""Bookmark Shelf: a local bookmark manager backed by SQLite.

Run:    python3 server.py
Open:   http://localhost:8765

Your bookmarks live in bookmarks.db, next to this script. Nothing leaves your
computer. Only the Python standard library is used (Python 3.8 or newer).

Options:
    python3 server.py --port 9000 --db /path/to/bookmarks.db
"""
import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
MAX_BODY = 10 * 1024 * 1024  # 10 MB, enough for tens of thousands of bookmarks

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id       TEXT PRIMARY KEY,
    url      TEXT NOT NULL,
    title    TEXT NOT NULL DEFAULT '',
    app      TEXT NOT NULL DEFAULT '',
    fav      INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_app ON bookmarks(app);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def clean(raw):
    """Validate one bookmark from the browser and return a DB row tuple."""
    if not isinstance(raw, dict):
        raise ValueError("bookmark must be an object")
    bid, url = raw.get("id"), raw.get("url")
    if not isinstance(bid, str) or not (0 < len(bid) <= 100):
        raise ValueError("bad id")
    if not isinstance(url, str) or not (0 < len(url) <= 4096):
        raise ValueError("bad url")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    title = str(raw.get("title") or "")[:500]
    app = str(raw.get("cat") or "")[:200]
    fav = 1 if raw.get("fav") else 0
    added = raw.get("at")
    added = int(added) if isinstance(added, (int, float)) else 0
    return (bid, url, title, app, fav, added)


UPSERT = (
    "INSERT INTO bookmarks (id, url, title, app, fav, added_at) VALUES (?,?,?,?,?,?) "
    "ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=excluded.title, "
    "app=excluded.app, fav=excluded.fav, added_at=excluded.added_at"
)


def make_handler(db_path, port):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BookmarkShelf/1.0"

        # --- helpers -------------------------------------------------
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def send_json(self, status, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def guard(self):
            """Block other websites from talking to this server (DNS rebinding / CSRF)."""
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send_json(403, {"error": "forbidden host"})
                return False
            origin = self.headers.get("Origin")
            if self.command != "GET" and origin and urlparse(origin).netloc not in allowed_hosts:
                self.send_json(403, {"error": "forbidden origin"})
                return False
            return True

        def read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(length) or b"null")

        # --- routes --------------------------------------------------
        def do_GET(self):
            if not self.guard():
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                data = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/bookmarks":
                with connect(db_path) as conn:
                    rows = conn.execute(
                        "SELECT id, url, title, app, fav, added_at FROM bookmarks"
                    ).fetchall()
                self.send_json(200, [
                    {"id": r["id"], "url": r["url"], "title": r["title"],
                     "cat": r["app"], "fav": bool(r["fav"]), "at": r["added_at"]}
                    for r in rows
                ])
            else:
                self.send_json(404, {"error": "not found"})

        def do_PUT(self):
            if not self.guard():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "bookmarks"]:
                return self.send_json(404, {"error": "not found"})
            try:
                raw = self.read_json()
                if isinstance(raw, dict):
                    raw["id"] = unquote(parts[2])
                row = clean(raw)
            except (ValueError, TypeError) as e:
                return self.send_json(400, {"error": str(e)})
            with connect(db_path) as conn:
                conn.execute(UPSERT, row)
            self.send_json(200, {"ok": True})

        def do_POST(self):
            if not self.guard():
                return
            if urlparse(self.path).path != "/api/bookmarks/bulk":
                return self.send_json(404, {"error": "not found"})
            try:
                raw = self.read_json()
                if not isinstance(raw, list):
                    raise ValueError("expected a list")
                rows = [clean(x) for x in raw]
            except (ValueError, TypeError) as e:
                return self.send_json(400, {"error": str(e)})
            with connect(db_path) as conn:  # one transaction for the whole import
                conn.executemany(UPSERT, rows)
            self.send_json(200, {"ok": True, "count": len(rows)})

        def do_DELETE(self):
            if not self.guard():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "bookmarks"]:
                return self.send_json(404, {"error": "not found"})
            with connect(db_path) as conn:
                conn.execute("DELETE FROM bookmarks WHERE id = ?", (unquote(parts[2]),))
            self.send_json(204)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Bookmark Shelf local server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=str(HERE / "bookmarks.db"))
    args = ap.parse_args()

    if not (HERE / "index.html").exists():
        sys.exit("index.html not found next to server.py")
    db_path = str(Path(args.db).expanduser().resolve())
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)

    # 127.0.0.1 only: other devices on your network cannot reach it.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(db_path, args.port))
    print(f"Bookmark Shelf is running at http://localhost:{args.port}")
    print(f"Database file: {db_path}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
