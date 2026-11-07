"""Sending over SMTP, with a clear answer to "did that email go or not?".

The SMTP conversation is driven step by step (MAIL, RCPT, DATA) instead of through
smtplib's sendmail(), so it is known whether the server could already have the message
when something goes wrong:

* a refusal or a dropped connection before DATA: the email certainly did not go (Refused);
* a dropped connection or timeout after DATA started: it may have gone (Uncertain).
"""

from __future__ import annotations

import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formataddr, formatdate, make_msgid

from mailrows.config import ConfigError, Settings
from mailrows.template import Rendered

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Refused(Exception):
    """The email was not accepted. Safe to try again later."""


class Uncertain(Exception):
    """The connection broke while the server was receiving the email. It may have been delivered."""


@dataclass
class SmtpSettings:
    host: str
    port: int
    security: str  # "ssl", "starttls" or "none"
    username: str
    password: str
    timeout: float = 30

    @classmethod
    def from_settings(cls, s: Settings, need_password: bool = True) -> "SmtpSettings":
        security = s.get("smtp", "security", "starttls").lower()
        if security not in ("ssl", "starttls", "none"):
            raise ConfigError("[smtp] security must be ssl, starttls or none.")
        username = s.get("smtp", "username", "")
        password = s.secret("smtp", "password") if (username and need_password) else s.get("smtp", "password", "")
        host = s.get("smtp", "host")
        if security == "none" and username and host.lower() not in LOCAL_HOSTS:
            raise ConfigError(
                "[smtp] security = none would send your password unencrypted. Use ssl or starttls "
                "(run 'mailrows smtp-check' to find the right setting)."
            )
        return cls(host=host, port=s.get_int("smtp", "port", 587), security=security,
                   username=username, password=password, timeout=s.get_float("smtp", "timeout", 30))


@dataclass
class Sender:
    from_email: str
    from_name: str = ""
    reply_to: str = ""
    bcc: str = ""

    @classmethod
    def from_settings(cls, s: Settings) -> "Sender":
        return cls(from_email=s.get("sender", "from_email"), from_name=s.get("sender", "from_name", ""),
                   reply_to=s.get("sender", "reply_to", ""), bcc=s.get("sender", "bcc", ""))


def connect(cfg: SmtpSettings) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        conn = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)
    else:
        conn = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        conn.ehlo()
        if cfg.security == "starttls":
            conn.starttls(context=context)
            conn.ehlo()
    if cfg.username:
        conn.login(cfg.username, cfg.password)
    return conn


def build_message(sender: Sender, msg: Rendered, to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = formataddr((sender.from_name, sender.from_email)) if sender.from_name else sender.from_email
    m["To"] = to or msg.to
    m["Subject"] = msg.subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=sender.from_email.rpartition("@")[2] or None)
    if sender.reply_to:
        m["Reply-To"] = sender.reply_to
    m.set_content(msg.text, cte=_cte(msg.text))
    if msg.html is not None:
        m.add_alternative(msg.html, subtype="html", cte=_cte(msg.html))
    return m


def _cte(text: str) -> str:
    """7bit when that is safe, else quoted-printable, so no server needs 8BITMIME support."""
    if text.isascii() and all(len(line) <= 900 for line in text.splitlines()):
        return "7bit"
    return "quoted-printable"


class Mailer:
    """One SMTP connection, reopened if the server drops it between emails."""

    def __init__(self, cfg: SmtpSettings, sender: Sender):
        self.cfg = cfg
        self.sender = sender
        self.conn: smtplib.SMTP | None = None

    def __enter__(self):
        self.conn = connect(self.cfg)
        return self

    def __exit__(self, kind, value, tb):
        if self.conn is not None:
            try:
                self.conn.quit()
            except (smtplib.SMTPException, OSError):
                pass
        self.conn = None

    def _ready(self) -> smtplib.SMTP:
        if self.conn is not None:
            try:
                code, _ = self.conn.noop()
                if code == 250:
                    return self.conn
            except (smtplib.SMTPException, OSError):
                pass
            try:
                self.conn.close()
            except (smtplib.SMTPException, OSError):
                pass
        self.conn = connect(self.cfg)
        return self.conn

    def send(self, message: EmailMessage, envelope_to: list[str]) -> None:
        """Send or raise Refused / Uncertain. BCC addresses go in envelope_to only, never in a header."""
        try:
            conn = self._ready()
        except (smtplib.SMTPException, OSError) as exc:
            raise Refused(f"could not connect to the mail server: {exc}") from exc
        data_started = False
        try:
            code, resp = conn.mail(self.sender.from_email)
            if code != 250:
                raise Refused(f"server refused the sender ({code} {resp!r})")
            accepted = 0
            for addr in envelope_to:
                code, resp = conn.rcpt(addr)
                if code in (250, 251):
                    accepted += 1
                elif addr == envelope_to[0]:
                    conn.rset()
                    raise Refused(f"server refused {addr} ({code} {_text(resp)})")
            if not accepted:
                raise Refused("server refused every recipient")
            data_started = True
            code, resp = conn.data(message.as_bytes(policy=SMTP_POLICY))
            if code != 250:
                raise Refused(f"server refused the message ({code} {_text(resp)})")
        except Refused:
            raise
        except smtplib.SMTPResponseException as exc:
            # a numbered reply from the server: it answered, and the answer was no
            raise Refused(f"server said {exc.smtp_code} {_text(exc.smtp_error)}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            self.conn = None
            if data_started:
                raise Uncertain(f"the connection broke while the server was receiving the email ({exc})") from exc
            raise Refused(f"the connection broke before the email was handed over ({exc})") from exc


def _text(resp) -> str:
    return resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)


def pause(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
