"""Append a line to a CSV file after each email (a simple local record for another tool).

    [csv_append]
    path = sent.csv
    columns = id, email, _sent_at
"""

from __future__ import annotations

import csv

from mailrows.config import ConfigError
from mailrows.hooks import EXTRA_VALUES, Hook, HookRow


class CsvAppender(Hook):
    name = "csv_append"

    def __init__(self, settings):
        super().__init__(settings)
        self.path = settings.resolve(settings.get("csv_append", "path"))
        self.columns = settings.get_list("csv_append", "columns") or ["_row_id", "_recipient", "_sent_at"]

    def describe(self):
        return f"csv_append: add a line to {self.path.name}"

    def check_columns(self, columns):
        unknown = [c for c in self.columns if c not in columns and c not in EXTRA_VALUES]
        if unknown:
            raise ConfigError(f"[csv_append] columns lists {', '.join(unknown)}, not in the spreadsheet.")

    def run(self, row: HookRow) -> None:
        merged = row.merged()
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self.columns)
            w.writerow([_safe(merged.get(c, "")) for c in self.columns])


def _safe(value: str) -> str:
    """Stop a spreadsheet program from running a value as a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
