"""Post-send hooks: something to do for each row after its email has gone.

A hook has two steps:

* prepare(rows) runs before ANY email is sent. It checks that the hook will be able to do
  its job for each row (for Airtable: that the matching record exists) and returns the
  rows it can't handle, which are then skipped. Raising HookError stops the whole run.
* run(row) runs after that row's email was sent. If it raises, the row is marked
  "sent, update pending" and a later run retries only the hook, never the email.

To add a target, subclass Hook, give it a unique name, and register it in HOOK_TYPES.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mailrows.config import ConfigError, Settings
from mailrows.template import fill, placeholders

EXTRA_VALUES = ("_row_id", "_recipient", "_subject", "_sent_at", "_sent_date")


class HookError(Exception):
    """The hook can't run at all (bad token, unknown table...). Nothing will be sent."""


@dataclass
class HookRow:
    row_id: str
    values: dict[str, str]
    recipient: str
    subject: str
    sent_at: str = ""
    scratch: dict = field(default_factory=dict)  # whatever prepare() wants to hand to run()

    def merged(self) -> dict[str, str]:
        extra = {
            "_row_id": self.row_id,
            "_recipient": self.recipient,
            "_subject": self.subject,
            "_sent_at": self.sent_at,
            "_sent_date": self.sent_at[:10],
        }
        return {**extra, **self.values}


class Hook:
    name = "hook"

    def __init__(self, settings: Settings):
        self.settings = settings

    def check_columns(self, columns: list[str]) -> None:
        """Raise ConfigError if a configured value uses an unknown {column}."""

    def prepare(self, rows: list[HookRow]) -> dict[str, str]:
        return {}

    def run(self, row: HookRow) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


def check_value_placeholders(where: str, values: dict[str, str], columns: list[str]) -> None:
    known = set(columns) | set(EXTRA_VALUES)
    for key, value in values.items():
        for name in placeholders(value, f"{where} {key}"):
            if name not in known:
                raise ConfigError(
                    f"{where} '{key}' uses {{{name}}}, which is not a spreadsheet column. "
                    f"Built-in values are {', '.join('{' + v + '}' for v in EXTRA_VALUES)}."
                )


def fill_values(values: dict[str, str], row: HookRow) -> dict[str, str]:
    merged = row.merged()
    return {k: fill(v, merged) for k, v in values.items()}


def load_hooks(settings: Settings) -> list[Hook]:
    from mailrows.hooks.airtable import UpdateAirtable
    from mailrows.hooks.csv_append import CsvAppender
    from mailrows.hooks.webhook import PostWebhook

    types = {h.name: h for h in (UpdateAirtable, PostWebhook, CsvAppender)}
    out = []
    for name in settings.get_list("hooks", "enabled"):
        if name not in types:
            raise ConfigError(f"[hooks] enabled lists '{name}'. Known hooks: {', '.join(types)}.")
        out.append(types[name](settings))
    return out
