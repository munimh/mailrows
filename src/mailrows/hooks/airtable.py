"""Update one Airtable record per sent email.

Config:

    [airtable]
    base_id = appXXXXXXXXXXXXXX       ; from the base's API page
    table = Attendees                 ; table name or ID
    match_field = Ticket ID           ; Airtable field to look the record up by
    match_column = id                 ; spreadsheet column holding that value
    token = ...                       ; or MAILROWS_AIRTABLE_TOKEN

    [airtable.fields]
    ; Airtable field = value. {column} takes the value from the spreadsheet row.
    Status = Invited
    Invite sent = {_sent_date}

Every matching record is found before any email is sent. Calls are kept under 5 per second
and retried with backoff when Airtable is busy.
"""

from __future__ import annotations

import urllib.parse

from mailrows.config import ConfigError
from mailrows.hooks import Hook, HookError, HookRow, check_value_placeholders, fill_values
from mailrows.hooks.http import HttpError, JsonClient
from mailrows.table import as_text

LOOKUP_BATCH = 25  # values per lookup request: keeps the URL short

HINTS = {
    401: "the token was not accepted. Create a new one at airtable.com/create/tokens",
    403: "the token can't use this base or table. Give it the data.records:read and "
         "data.records:write scopes and access to this base",
    404: "the base or table was not found. Check base_id and table under [airtable]",
    422: "Airtable turned the request down. Check match_field and the names under [airtable.fields]",
}


def quote_formula_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class UpdateAirtable(Hook):
    name = "airtable"

    def __init__(self, settings, sleep=None):
        super().__init__(settings)
        s = settings
        self.base = s.get("airtable", "base_id")
        self.table = s.get("airtable", "table")
        self.match_field = s.get("airtable", "match_field")
        self.match_column = s.get("airtable", "match_column")
        if any(ch in self.match_field for ch in "{}"):
            raise ConfigError("[airtable] match_field can't contain { or }.")
        self.fields = s.items("airtable.fields")
        if not self.fields:
            raise ConfigError("[airtable.fields] is empty: list at least one 'Field name = value'.")
        self.api = s.get("airtable", "api_url", "https://api.airtable.com/v0").rstrip("/")
        token = s.secret("airtable", "token")
        extra = {"sleep": sleep} if sleep else {}
        self.client = JsonClient({"Authorization": f"Bearer {token}"}, per_second=4.5,
                                 retries=s.get_int("airtable", "retries", 5),
                                 backoff=s.get_float("airtable", "backoff_seconds", 1.0), **extra)

    def describe(self):
        names = ", ".join(self.fields)
        return f"airtable: set {names} on the record whose '{self.match_field}' matches '{self.match_column}'"

    def _url(self, record_id: str = "") -> str:
        path = f"{self.api}/{urllib.parse.quote(self.base, safe='')}/{urllib.parse.quote(self.table, safe='')}"
        return f"{path}/{urllib.parse.quote(record_id, safe='')}" if record_id else path

    def _request(self, method: str, url: str, payload=None):
        try:
            return self.client.call(method, url, payload)
        except HttpError as exc:
            hint = HINTS.get(exc.status or 0)
            detail = hint or (exc.body[:200] if exc.body else str(exc))
            raise HookError(f"Airtable: {exc} - {detail}") from None

    def check_columns(self, columns):
        if self.match_column not in columns:
            raise ConfigError(f"[airtable] match_column '{self.match_column}' is not a spreadsheet column.")
        check_value_placeholders("[airtable.fields]", self.fields, columns)

    def prepare(self, rows: list[HookRow]) -> dict[str, str]:
        wanted: dict[str, list[HookRow]] = {}
        problems: dict[str, str] = {}
        for row in rows:
            value = row.values.get(self.match_column, "").strip()
            if not value:
                problems[row.row_id] = f"'{self.match_column}' is empty, so no Airtable record can be matched"
            else:
                wanted.setdefault(value, []).append(row)
        found: dict[str, list[str]] = {v: [] for v in wanted}
        values = list(wanted)
        for i in range(0, len(values), LOOKUP_BATCH):
            chunk = values[i:i + LOOKUP_BATCH]
            # &'' turns numbers into text, so 101 and "101" both match
            terms = [f"{{{self.match_field}}}&''={quote_formula_string(v)}" for v in chunk]
            formula = terms[0] if len(terms) == 1 else "OR(" + ",".join(terms) + ")"
            offset = None
            while True:
                params = [("filterByFormula", formula), ("fields[]", self.match_field), ("pageSize", "100")]
                if offset:
                    params.append(("offset", offset))
                page = self._request("GET", self._url() + "?" + urllib.parse.urlencode(params))
                for record in page.get("records") or []:
                    got = (record.get("fields") or {}).get(self.match_field)
                    if isinstance(got, list):
                        got = got[0] if got else None
                    key = as_text(got)
                    if key in found:
                        found[key].append(record["id"])
                offset = page.get("offset") or None
                if not offset:
                    break
        for value, matched in found.items():
            for row in wanted[value]:
                if not matched:
                    problems[row.row_id] = f"no Airtable record has {self.match_field} = {value}"
                elif len(matched) > 1:
                    problems[row.row_id] = f"{len(matched)} Airtable records have {self.match_field} = {value}"
                else:
                    row.scratch["airtable_id"] = matched[0]
        return problems

    def run(self, row: HookRow) -> None:
        record_id = row.scratch.get("airtable_id")
        if not record_id:
            raise HookError("the Airtable record was not looked up first")
        self._request("PATCH", self._url(record_id), {"fields": fill_values(self.fields, row), "typecast": True})
