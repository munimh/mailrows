"""The send log: a small SQLite file that is the final word on what has been sent.

The order of events for one email is what makes reruns safe:

1. claim():     a 'sending' record is committed BEFORE the email is handed to the server.
2. the email is sent.
3. mark_sent(): the record becomes 'sent'.

If the process dies between 1 and 3, the record stays 'sending'. A rerun sees it and does
not send again; a person checks their Sent folder and runs 'mailrows resolve'. If the
server clearly refused the email, release() removes the claim so a rerun can try again.

Each sent email also remembers which post-send hooks it still owes (an Airtable update,
for example). Those are retried on later runs without sending the email again.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    row_id      TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('sending', 'sent')),
    hooks       TEXT NOT NULL DEFAULT '',
    claimed_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX IF NOT EXISTS sends_by_fingerprint ON sends (fingerprint);
CREATE TABLE IF NOT EXISTS hooks_done (
    row_id  TEXT NOT NULL,
    hook    TEXT NOT NULL,
    done_at TEXT NOT NULL,
    PRIMARY KEY (row_id, hook)
);
CREATE TABLE IF NOT EXISTS events (
    at      TEXT NOT NULL,
    row_id  TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Entry:
    row_id: str
    fingerprint: str
    recipient: str
    state: str
    hooks: tuple[str, ...]
    done: frozenset[str]
    sent_at: str | None

    @property
    def pending_hooks(self) -> list[str]:
        return [h for h in self.hooks if h not in self.done]


class ClaimTaken(Exception):
    pass


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    @classmethod
    def read_only(cls, path: Path) -> "ReadOnlyLedger":
        return ReadOnlyLedger(path)

    def close(self) -> None:
        self.db.close()

    # ---- reading

    def entry(self, row_id: str) -> Entry | None:
        r = self.db.execute(
            "SELECT row_id, fingerprint, recipient, state, hooks, sent_at FROM sends WHERE row_id = ?", (row_id,)
        ).fetchone()
        if r is None:
            return None
        done = {h for (h,) in self.db.execute("SELECT hook FROM hooks_done WHERE row_id = ?", (row_id,))}
        hooks = tuple(h for h in r[4].split(",") if h)
        return Entry(r[0], r[1], r[2], r[3], hooks, frozenset(done), r[5])

    def all_entries(self) -> dict[str, Entry]:
        ids = [r for (r,) in self.db.execute("SELECT row_id FROM sends")]
        return {i: self.entry(i) for i in ids}

    def by_fingerprint(self, fingerprint: str) -> str | None:
        r = self.db.execute("SELECT row_id FROM sends WHERE fingerprint = ? LIMIT 1", (fingerprint,)).fetchone()
        return r[0] if r else None

    # ---- writing

    def claim(self, row_id: str, fingerprint: str, recipient: str, hooks: list[str]) -> None:
        """Record the intent to send, atomically. Raises ClaimTaken if any run got there first."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            exists = self.db.execute(
                "SELECT 1 FROM sends WHERE row_id = ? OR fingerprint = ?", (row_id, fingerprint)
            ).fetchone()
            if exists:
                self.db.execute("ROLLBACK")
                raise ClaimTaken(row_id)
            self.db.execute(
                "INSERT INTO sends (row_id, fingerprint, recipient, state, hooks, claimed_at) VALUES (?,?,?,?,?,?)",
                (row_id, fingerprint, recipient, "sending", ",".join(hooks), now()),
            )
            self._event(row_id, "claimed")
            self.db.execute("COMMIT")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def mark_sent(self, row_id: str) -> None:
        with self._tx():
            self.db.execute("UPDATE sends SET state = 'sent', sent_at = ? WHERE row_id = ?", (now(), row_id))
            self._event(row_id, "sent")

    def release(self, row_id: str, why: str) -> None:
        """The server refused the email, so it did not go: allow a later retry."""
        with self._tx():
            self.db.execute("DELETE FROM sends WHERE row_id = ? AND state = 'sending'", (row_id,))
            self._event(row_id, "refused", why)

    def mark_hook_done(self, row_id: str, hook: str) -> None:
        with self._tx():
            self.db.execute(
                "INSERT OR REPLACE INTO hooks_done (row_id, hook, done_at) VALUES (?,?,?)", (row_id, hook, now())
            )
            self._event(row_id, f"hook {hook} done")

    def note(self, row_id: str, event: str, detail: str = "") -> None:
        with self._tx():
            self._event(row_id, event, detail)

    def resolve(self, row_id: str, was_sent: bool) -> Entry | None:
        """A person checked their Sent folder for an email whose outcome wasn't known."""
        e = self.entry(row_id)
        if e is None or e.state != "sending":
            return e
        with self._tx():
            if was_sent:
                self.db.execute("UPDATE sends SET state = 'sent', sent_at = ? WHERE row_id = ?", (now(), row_id))
                self._event(row_id, "resolved as sent")
            else:
                self.db.execute("DELETE FROM sends WHERE row_id = ?", (row_id,))
                self._event(row_id, "resolved as not sent")
        return self.entry(row_id)

    # ---- helpers

    def _event(self, row_id: str, event: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events (at, row_id, event, detail) VALUES (?,?,?,?)",
                        (now(), row_id, event, detail[:500]))

    def _tx(self):
        db = self.db

        class _T:
            def __enter__(self):
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, kind, *_):
                db.execute("COMMIT" if kind is None else "ROLLBACK")
                return False

        return _T()


class ReadOnlyLedger:
    """For dry runs and test sends: reads the log if it exists, never creates or changes it."""

    def __init__(self, path: Path):
        self._inner = None
        if path.exists():
            uri = path.resolve().as_uri() + "?mode=ro"
            self._inner = Ledger.__new__(Ledger)
            self._inner.path = path
            self._inner.db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)

    def entry(self, row_id):
        return self._inner.entry(row_id) if self._inner else None

    def all_entries(self):
        return self._inner.all_entries() if self._inner else {}

    def by_fingerprint(self, fingerprint):
        return self._inner.by_fingerprint(fingerprint) if self._inner else None

    def close(self):
        if self._inner:
            self._inner.db.close()
