"""Reading rows from .xlsx or .csv, and writing the status column back.

Every write re-opens the file, finds the row by its ID (so sorting or inserting rows in
between is fine), and saves through a temporary file that then replaces the original, so
an interrupted save can't leave a half-written file.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
from dataclasses import dataclass, field
from pathlib import Path


class TableError(Exception):
    pass


class FileLocked(TableError):
    """The file can't be written, usually because it is open in Excel."""


@dataclass
class Row:
    line: int  # the row number a person sees in Excel (header is 1)
    values: dict[str, str] = field(default_factory=dict)


def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date)):
        if isinstance(value, dt.datetime) and value.time() != dt.time():
            return value.isoformat(sep=" ")
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def _replace_atomically(path: Path, write) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except PermissionError as exc:
        raise FileLocked(f"{path.name} can't be saved. Is it open in Excel or another program?") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class Table:
    """One spreadsheet. Subclasses handle the file format."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def open(path: Path, sheet: str = "", delimiter: str = "") -> "Table":
        if not path.exists():
            raise TableError(f"Spreadsheet {path} not found.")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return CsvTable(path, delimiter)
        if suffix == ".xlsx":
            return XlsxTable(path, sheet)
        raise TableError(f"{path.name}: use an .xlsx or .csv file.")

    def read(self) -> tuple[list[str], list[Row]]:
        raise NotImplementedError

    def set_values(self, id_column: str, column: str, updates: dict[str, str]) -> int:
        """Write column=value on the row whose id_column is each key. Returns rows written."""
        raise NotImplementedError

    def check_writable(self) -> None:
        try:
            with open(self.path, "r+b"):
                pass
        except PermissionError:
            raise FileLocked(f"{self.path.name} is open in another program (often Excel). Close it and try again.")


def _headers(raw: list) -> list[str]:
    names = [as_text(v) for v in raw]
    while names and not names[-1]:
        names.pop()
    seen = set()
    for n in names:
        if n and n in seen:
            raise TableError(f"Two columns are both called '{n}'. Rename one.")
        seen.add(n)
    return names


class CsvTable(Table):
    def __init__(self, path: Path, delimiter: str = ""):
        super().__init__(path)
        self.delimiter = delimiter

    def _load(self):
        raw = self.path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise TableError(f"{self.path.name} is not UTF-8. Save it as 'CSV UTF-8' and try again.") from None
        delim = self.delimiter
        if not delim:
            first = text.split("\n", 1)[0]
            delim = max([",", ";", "\t"], key=first.count)
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
        return rows, delim, bom

    def read(self):
        rows, _, _ = self._load()
        if not rows:
            raise TableError(f"{self.path.name} is empty.")
        headers = _headers(rows[0])
        out = []
        for i, raw in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in raw):
                continue
            values = {h: (raw[j].strip() if j < len(raw) else "") for j, h in enumerate(headers) if h}
            out.append(Row(line=i, values=values))
        return headers, out

    def set_values(self, id_column, column, updates):
        rows, delim, bom = self._load()
        headers = _headers(rows[0])
        if column not in headers:
            raise TableError(f"{self.path.name} has no '{column}' column to write to.")
        id_at, col_at = headers.index(id_column), headers.index(column)
        written = 0
        for raw in rows[1:]:
            key = raw[id_at].strip() if id_at < len(raw) else ""
            if key in updates:
                while len(raw) <= col_at:
                    raw.append("")
                raw[col_at] = updates[key]
                written += 1

        def write(tmp: Path):
            with open(tmp, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
                csv.writer(f, delimiter=delim).writerows(rows)

        _replace_atomically(self.path, write)
        return written


class XlsxTable(Table):
    def __init__(self, path: Path, sheet: str = ""):
        super().__init__(path)
        self.sheet = sheet

    def _book(self, data_only: bool):
        import openpyxl

        try:
            return openpyxl.load_workbook(self.path, data_only=data_only)
        except PermissionError:
            raise FileLocked(f"{self.path.name} can't be opened. Is it open in another program?") from None
        except Exception as exc:  # openpyxl raises several kinds for a damaged file
            raise TableError(f"{self.path.name} can't be read as an Excel file ({exc}).") from None

    def _ws(self, wb):
        if self.sheet:
            if self.sheet not in wb.sheetnames:
                raise TableError(f"{self.path.name} has no tab called '{self.sheet}'. Tabs: {', '.join(wb.sheetnames)}")
            return wb[self.sheet]
        return wb.worksheets[0]

    def read(self):
        # data_only gives the last calculated value of formula cells, which is what a person sees
        wb = self._book(data_only=True)
        try:
            ws = self._ws(wb)
            it = ws.iter_rows(values_only=True)
            first = next(it, None)
            if first is None:
                raise TableError(f"{self.path.name} is empty.")
            headers = _headers(list(first))
            out = []
            for i, raw in enumerate(it, start=2):
                cells = [as_text(v) for v in raw]
                if not any(cells):
                    continue
                values = {h: (cells[j] if j < len(cells) else "") for j, h in enumerate(headers) if h}
                out.append(Row(line=i, values=values))
            return headers, out
        finally:
            wb.close()

    def set_values(self, id_column, column, updates):
        # opened without data_only, so formulas are kept when the file is saved again
        wb = self._book(data_only=False)
        try:
            ws = self._ws(wb)
            headers = _headers([c.value for c in ws[1]])
            if column not in headers:
                raise TableError(f"{self.path.name} has no '{column}' column to write to.")
            id_at, col = headers.index(id_column), headers.index(column) + 1
            written = 0
            for cells in ws.iter_rows(min_row=2):
                if id_at >= len(cells):
                    continue
                key = as_text(cells[id_at].value)
                if key in updates:
                    cell = ws.cell(row=cells[id_at].row, column=col)
                    cell.value = updates[key]
                    cell.data_type = "s"  # always plain text, never a formula
                    written += 1
            _replace_atomically(self.path, wb.save)
            return written
        finally:
            wb.close()
