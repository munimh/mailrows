"""Test fixtures: a fake SMTP server and a fake Airtable API, both real servers on 127.0.0.1."""

from __future__ import annotations

import csv
import email
import json
import re
import socketserver
import threading
import time
import urllib.parse
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mailrows import config, run

SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------- fake SMTP

class SmtpHandler(socketserver.StreamRequestHandler):
    def reply(self, text: str) -> None:
        self.wfile.write((text + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        srv: FakeSmtp = self.server
        self.reply("220 fake.test ESMTP ready")
        mail_from, rcpts = None, []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            verb = cmd.split(" ", 1)[0].upper()
            if verb == "EHLO":
                self.wfile.write(b"250-fake.test\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                self.wfile.flush()
            elif verb == "HELO":
                self.reply("250 fake.test")
            elif verb == "AUTH":
                if srv.password_ok:
                    self.reply("235 2.7.0 Authentication successful")
                else:
                    self.reply("535 5.7.8 Authentication failed")
            elif verb == "MAIL":
                mail_from, rcpts = cmd[10:].strip("<> "), []
                self.reply("250 OK")
            elif verb == "RCPT":
                addr = cmd[8:].strip().strip("<>")
                if addr in srv.refuse:
                    self.reply("550 5.1.1 No such user")
                else:
                    rcpts.append(addr)
                    self.reply("250 OK")
            elif verb == "DATA":
                self.reply("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    part = self.rfile.readline()
                    if not part or part == b".\r\n":
                        break
                    chunks.append(part[1:] if part.startswith(b"..") else part)
                raw = b"".join(chunks)
                with srv.lock:
                    srv.messages.append((mail_from, list(rcpts), raw))
                    drop = srv.drop_after_data > 0
                    if drop:
                        srv.drop_after_data -= 1
                if drop:
                    return  # hang up without confirming: the client can't know if it went
                self.reply("250 OK queued")
            elif verb in ("RSET", "NOOP"):
                self.reply("250 OK")
            elif verb == "QUIT":
                self.reply("221 Bye")
                return
            else:
                self.reply("502 Command not implemented")


class FakeSmtp(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), SmtpHandler)
        self.lock = threading.Lock()
        self.messages: list[tuple[str, list[str], bytes]] = []
        self.refuse: set[str] = set()
        self.drop_after_data = 0
        self.password_ok = True

    @property
    def port(self) -> int:
        return self.server_address[1]

    def parsed(self):
        return [email.message_from_bytes(raw, policy=policy.default) for _, _, raw in self.messages]


@pytest.fixture
def smtp():
    srv = FakeSmtp()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- fake Airtable

class FakeAirtableState:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.calls: list[tuple[float, str, str]] = []
        self.patches: list[tuple[str, dict]] = []
        self.fail_patch: set[str] = set()  # record ids whose PATCH always fails with 503
        self.busy_once = 0  # answer this many requests with 429 first
        self.token = "test-token"
        self.lock = threading.Lock()

    def add(self, rec_id: str, **fields):
        self.records[rec_id] = dict(fields)


class FakeAirtableHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _gate(self) -> bool:
        st: FakeAirtableState = self.server.state
        with st.lock:
            st.calls.append((time.monotonic(), self.command, self.path))
            if self.headers.get("Authorization") != f"Bearer {st.token}":
                self._send(401, {"error": "AUTHENTICATION_REQUIRED"})
                return False
            if st.busy_once > 0:
                st.busy_once -= 1
                self._send(429, {"error": "RATE_LIMITED"})
                return False
        return True

    def do_GET(self):
        if not self._gate():
            return
        st = self.server.state
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        formula = q.get("filterByFormula", [""])[0]
        field = re.search(r"\{([^}]*)\}", formula).group(1)
        wanted = {re.sub(r'\\(.)', r"\1", v) for v in re.findall(r'="((?:[^"\\]|\\.)*)"', formula)}
        out = [{"id": rid, "fields": {field: f[field]}} for rid, f in st.records.items()
               if field in f and str(f[field]) in wanted]
        self._send(200, {"records": out})

    def do_PATCH(self):
        if not self._gate():
            return
        st = self.server.state
        rec_id = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[1]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if rec_id in st.fail_patch:
            self._send(503, {"error": "SERVICE_UNAVAILABLE"})
            return
        if rec_id not in st.records:
            self._send(404, {"error": "NOT_FOUND"})
            return
        with st.lock:
            st.records[rec_id].update(body["fields"])
            st.patches.append((rec_id, body["fields"]))
        self._send(200, {"id": rec_id, "fields": st.records[rec_id]})

    def do_POST(self):  # used as a webhook receiver
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with st.lock:
            st.calls.append((time.monotonic(), "POST", self.path))
            st.patches.append(("webhook", body))
        self._send(200, {"ok": True})


@pytest.fixture
def airtable():
    st = FakeAirtableState()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAirtableHandler)
    srv.state = st
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    st.url = f"http://127.0.0.1:{srv.server_address[1]}/v0"
    yield st
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- a project folder

HEADERS = ["id", "name", "email", "topic", "status"]
ROWS = [
    ["A1", "Alex Example", "alex@example.com", "Gardening", ""],
    ["A2", "Blair Example", "blair@example.com", "Cycling", ""],
    ["A3", "Casey Example", "casey@example.com", "Baking", ""],
]
TEMPLATE = "Subject: News about {topic}\nHello {name},\n\nHere is your {topic} update.\n"


class Project:
    def __init__(self, folder: Path, smtp_port: int):
        self.folder = folder
        self.smtp_port = smtp_port
        self.sheet = folder / "people.csv"
        self.config = folder / "config.ini"
        self.log = folder / "mailrows-log.sqlite3"
        self.lines: list[str] = []
        self.extra: dict[str, dict[str, str]] = {}

    def write_rows(self, rows, headers=HEADERS):
        if self.sheet.suffix == ".xlsx":
            import openpyxl

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(headers)
            for r in rows:
                ws.append(r)
            wb.save(self.sheet)
        else:
            with open(self.sheet, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(rows)

    def read_rows(self) -> list[dict[str, str]]:
        from mailrows.table import Table

        _, rows = Table.open(self.sheet).read()
        return [r.values for r in rows]

    def status(self) -> dict[str, str]:
        return {r["id"]: r.get("status", "") for r in self.read_rows()}

    def write_config(self, hooks: str = "", **sections):
        base = {
            "input": {"file": self.sheet.name, "id_column": "id", "email_column": "email",
                      "status_column": "status", "required_columns": "name, email"},
            "template": {"text": "template.txt"},
            "smtp": {"host": "127.0.0.1", "port": str(self.smtp_port), "security": "none", "timeout": "5"},
            "sender": {"from_name": "Example Club", "from_email": "club@example.com",
                       "reply_to": "reply@example.com", "delay_seconds": "0"},
            "hooks": {"enabled": hooks},
        }
        for name, values in sections.items():
            base.setdefault(name.replace("__", "."), {}).update(values)
        text = []
        for name, values in base.items():
            text.append(f"[{name}]")
            text += [f"{k} = {v}" for k, v in values.items()]
            text.append("")
        self.config.write_text("\n".join(text), encoding="utf-8")

    def settings(self):
        return config.load(self.config)

    def run(self, mode=run.REAL, test_to="", answer="yes"):
        self.lines = []
        asked = []

        def ask(q):
            asked.append(q)
            return answer

        code = run.send_command(self.settings(), mode, test_to=test_to, ask=ask,
                                out=lambda *a, end="\n": self.lines.append(" ".join(map(str, a))))
        self.asked = asked
        return code

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def snapshot(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.folder.iterdir()) if p.is_file()}


@pytest.fixture
def project(tmp_path, smtp):
    p = Project(tmp_path, smtp.port)
    p.write_rows(ROWS)
    (tmp_path / "template.txt").write_text(TEMPLATE, encoding="utf-8")
    p.write_config()
    return p


@pytest.fixture(autouse=True)
def no_mailrows_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("MAILROWS_"):
            monkeypatch.delenv(k)
