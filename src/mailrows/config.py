"""Settings: an INI file, with any value overridable by an environment variable.

Environment variables win over the file. The name is MAILROWS_<SECTION>_<KEY> in capitals,
with dots and dashes in the section name turned into underscores, for example
MAILROWS_SMTP_PASSWORD or MAILROWS_AIRTABLE_TOKEN.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """A setting is missing or wrong. The message says which and how to fix it."""


PLACEHOLDER_MARKERS = ("CHANGE_ME", "YOUR_", "<", "xxxxxxxx")


def env_name(section: str, key: str) -> str:
    clean = section.replace(".", "_").replace("-", "_")
    return f"MAILROWS_{clean}_{key}".upper()


def looks_unset(value: str) -> bool:
    v = value.strip()
    return not v or any(m.lower() in v.lower() for m in PLACEHOLDER_MARKERS)


@dataclass
class Settings:
    path: Path
    parser: configparser.ConfigParser = field(repr=False)

    @property
    def folder(self) -> Path:
        return self.path.parent

    def get(self, section: str, key: str, default: str | None = None) -> str:
        from_env = os.environ.get(env_name(section, key))
        if from_env is not None:
            return from_env.strip()
        if self.parser.has_option(section, key):
            return self.parser.get(section, key).strip()
        if default is None:
            raise ConfigError(
                f"Missing setting [{section}] {key} in {self.path.name} "
                f"(or set the environment variable {env_name(section, key)})."
            )
        return default

    def secret(self, section: str, key: str) -> str:
        """A password or token. Must be set, and not left as the example placeholder."""
        value = self.get(section, key, "")
        if looks_unset(value):
            raise ConfigError(
                f"[{section}] {key} is not set. Put it in {self.path.name} (which must stay out of git) "
                f"or set the environment variable {env_name(section, key)}."
            )
        return value

    def get_int(self, section: str, key: str, default: int) -> int:
        raw = self.get(section, key, str(default))
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"[{section}] {key} must be a whole number, not '{raw}'.") from None

    def get_float(self, section: str, key: str, default: float) -> float:
        raw = self.get(section, key, str(default))
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"[{section}] {key} must be a number, not '{raw}'.") from None
        if value < 0:
            raise ConfigError(f"[{section}] {key} can't be negative.")
        return value

    def get_list(self, section: str, key: str) -> list[str]:
        raw = self.get(section, key, "")
        return [part.strip() for part in raw.split(",") if part.strip()]

    def items(self, section: str) -> dict[str, str]:
        """Every key in a section, in file order, keeping the key's original capitals."""
        if not self.parser.has_section(section):
            return {}
        return {k: self.get(section, k) for k in self.parser.options(section)}

    def resolve(self, value: str) -> Path:
        """A path from the config, relative to the config file's folder."""
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.folder / p)


def load(path: str | Path) -> Settings:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file {path} not found. Copy config.example.ini to config.ini and fill it in, "
            "or run 'mailrows init' to create a starter folder."
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep field names like "Last emailed" exactly as written
    try:
        parser.read(path, encoding="utf-8-sig")
    except (configparser.Error, UnicodeDecodeError) as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ConfigError(f"{path.name} can't be read: {first_line}") from None
    return Settings(path=path.resolve(), parser=parser)
