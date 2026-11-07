"""POST a small JSON document to a URL after each email.

    [webhook]
    url = https://hooks.example.com/mailrows
    ; optional header, e.g. for a shared secret. The value can come from MAILROWS_WEBHOOK_HEADER_VALUE
    header_name = X-Mailrows-Token
    header_value = CHANGE_ME
    ; which columns to include (default: all)
    columns = id, email

The body is {"row_id", "recipient", "subject", "sent_at", "row": {column: value}}.
A receiver should treat row_id as the idempotency key: a retry can deliver the same row twice.
"""

from __future__ import annotations

import urllib.parse

from mailrows.config import ConfigError, looks_unset
from mailrows.hooks import Hook, HookError, HookRow
from mailrows.hooks.http import HttpError, JsonClient

def _is_local(url: str) -> bool:
    return urllib.parse.urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1")


class PostWebhook(Hook):
    name = "webhook"

    def __init__(self, settings, sleep=None):
        super().__init__(settings)
        s = settings
        self.url = s.get("webhook", "url")
        if not self.url.lower().startswith(("https://", "http://")):
            raise ConfigError("[webhook] url must start with https:// (or http:// for a local test).")
        headers = {"User-Agent": "mailrows"}
        name = s.get("webhook", "header_name", "")
        if name:
            value = s.get("webhook", "header_value", "")
            if looks_unset(value):
                raise ConfigError("[webhook] header_value is not set (or set MAILROWS_WEBHOOK_HEADER_VALUE).")
            headers[name] = value
            if self.url.lower().startswith("http://") and not _is_local(self.url):
                raise ConfigError("[webhook] url uses http://, which would send header_value unencrypted. "
                                  "Use https:// (http:// is only allowed for localhost).")
        self.columns = s.get_list("webhook", "columns")
        extra = {"sleep": sleep} if sleep else {}
        self.client = JsonClient(headers, per_second=s.get_float("webhook", "per_second", 5),
                                 retries=s.get_int("webhook", "retries", 4), **extra)

    def describe(self):
        return f"webhook: POST to {self.url.split('?')[0]}"

    def check_columns(self, columns):
        unknown = [c for c in self.columns if c not in columns]
        if unknown:
            raise ConfigError(f"[webhook] columns lists {', '.join(unknown)}, not in the spreadsheet.")

    def run(self, row: HookRow) -> None:
        values = {c: row.values.get(c, "") for c in self.columns} if self.columns else dict(row.values)
        body = {"row_id": row.row_id, "recipient": row.recipient, "subject": row.subject,
                "sent_at": row.sent_at, "row": values}
        try:
            self.client.call("POST", self.url, body)
        except HttpError as exc:
            raise HookError(f"webhook: {exc}") from None
