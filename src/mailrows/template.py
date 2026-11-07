"""Email templates.

A template file starts with a line "Subject: ..." and the rest is the body. Placeholders
are {column name}, matched exactly against the spreadsheet headings. Write {{ or }} for a
literal brace. An optional HTML file has the same placeholders (and no subject line); its
values are HTML-escaped.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from pathlib import Path

SUBJECT_LINE = re.compile(r"\s*subject\s*:(.*)$", re.IGNORECASE)
TOKEN = re.compile(r"\{\{|\}\}|\{([^{}\n]*)\}|[{}]")


class TemplateError(Exception):
    pass


@dataclass(frozen=True)
class Rendered:
    to: str
    subject: str
    text: str
    html: str | None

    def fingerprint(self) -> str:
        """Hash of exactly what the recipient gets. Same inputs, same hash."""
        h = hashlib.sha256()
        for part in (self.to.strip().lower(), self.subject, self.text, self.html or ""):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


def placeholders(text: str, where: str) -> list[str]:
    """Names used in a template, in order. Raises TemplateError on a stray brace."""
    names = []
    for m in TOKEN.finditer(text):
        tok = m.group(0)
        if tok in ("{{", "}}"):
            continue
        if tok in ("{", "}"):
            line = text.count("\n", 0, m.start()) + 1
            raise TemplateError(f"{where}, line {line}: a lone '{tok}'. Write '{tok}{tok}' for a literal brace.")
        name = m.group(1).strip()
        if not name:
            line = text.count("\n", 0, m.start()) + 1
            raise TemplateError(f"{where}, line {line}: empty placeholder {{}}.")
        names.append(name)
    return names


def fill(text: str, values: dict[str, str], escape: bool = False) -> str:
    def swap(m: re.Match) -> str:
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        value = values[m.group(1).strip()]
        return html.escape(value) if escape else value

    return TOKEN.sub(swap, text)


@dataclass
class Template:
    subject: str
    body: str
    html_body: str | None = None
    source: str = "template"

    @classmethod
    def load(cls, text_path: Path, html_path: Path | None = None) -> "Template":
        if not text_path.exists():
            raise TemplateError(f"Template file {text_path} not found.")
        raw = text_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        first, _, rest = raw.partition("\n")
        head = SUBJECT_LINE.match(first)
        if head is None:
            raise TemplateError(f"{text_path.name}: the first line must start with 'Subject:'.")
        subject = head.group(1).strip()
        if not subject:
            raise TemplateError(f"{text_path.name}: the subject line is empty.")
        html_body = None
        if html_path is not None:
            if not html_path.exists():
                raise TemplateError(f"HTML template {html_path} not found.")
            html_body = html_path.read_text(encoding="utf-8-sig")
        return cls(subject=subject, body=rest.lstrip("\n"), html_body=html_body, source=text_path.name)

    def parts(self) -> list[tuple[str, str]]:
        out = [("subject line", self.subject), (self.source, self.body)]
        if self.html_body is not None:
            out.append(("HTML template", self.html_body))
        return out

    def names(self) -> set[str]:
        found: set[str] = set()
        for where, text in self.parts():
            found.update(placeholders(text, where))
        return found

    def check_columns(self, columns: list[str]) -> None:
        """Stop before anything is sent if the template uses a column the sheet doesn't have."""
        known = set(columns)
        unknown = sorted(self.names() - known)
        if unknown:
            listed = ", ".join("{" + n + "}" for n in unknown)
            raise TemplateError(
                f"The template uses {listed}, which {'is' if len(unknown) == 1 else 'are'} not a column "
                f"in the spreadsheet. Columns are: {', '.join(columns)}. "
                "Placeholders must match a heading exactly, including capitals and spaces."
            )

    def render(self, to: str, values: dict[str, str]) -> Rendered:
        subject = " ".join(fill(self.subject, values).split())  # no line breaks in a header
        text = fill(self.body, values)
        html_out = fill(self.html_body, values, escape=True) if self.html_body is not None else None
        return Rendered(to=to, subject=subject, text=text, html=html_out)
