"""Find which port and security setting a mail server accepts, without sending anything."""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass

COMBOS = [
    (465, "ssl"),
    (587, "starttls"),
    (25, "starttls"),
    (2525, "starttls"),
    (25, "none"),
    (2525, "none"),
]


@dataclass
class Result:
    port: int
    security: str
    connected: bool = False
    logged_in: bool | None = None  # None: login not tried
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.connected and self.logged_in is not False


def explain(exc: BaseException, port: int, security: str) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("timed out. Something between you and the server is blocking this port: usually a firewall, "
                "antivirus 'mail shield', or a network (office, hotel, ISP) that blocks outgoing mail ports")
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused: the server isn't accepting mail on this port"
    if isinstance(exc, socket.gaierror):
        return "the host name could not be found. Check [smtp] host for typos"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"the server's certificate is not valid for this host name ({exc.verify_message})"
    if isinstance(exc, ssl.SSLError):
        if security == "ssl":
            return "SSL handshake failed: this port probably expects STARTTLS or plain SMTP, not SSL"
        return f"encryption failed to start ({exc.reason or exc})"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "the server doesn't offer STARTTLS on this port"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "connected, but the username or password was refused. Some providers need an 'app password'"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        if security != "ssl" and port == 465:
            return "the server hung up: port 465 normally needs security = ssl"
        return "the server closed the connection straight away"
    if isinstance(exc, ConnectionResetError):
        return "the connection was reset, often by antivirus or a firewall inspecting mail traffic"
    return f"{type(exc).__name__}: {exc}"


def try_one(host: str, port: int, security: str, username: str, password: str, timeout: float) -> Result:
    res = Result(port, security)
    conn = None
    try:
        context = ssl.create_default_context()
        if security == "ssl":
            conn = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            conn = smtplib.SMTP(host, port, timeout=timeout)
        conn.ehlo()
        if security == "starttls":
            conn.starttls(context=context)
            conn.ehlo()
        res.connected = True
        if not username:
            res.message = "connected (no username set, so login was not tested)"
        elif security == "none":
            res.message = "connected, but login was not tried: it would send your password unencrypted"
        elif not conn.has_extn("auth"):
            res.logged_in = False
            res.message = "connected, but the server doesn't offer login here"
        else:
            conn.login(username, password)
            res.logged_in = True
            res.message = "connected and logged in"
    except Exception as exc:  # every failure becomes a readable line
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            res.logged_in = False
        res.message = explain(exc, port, security)
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    return res


def check(host: str, username: str, password: str, timeout: float = 10, combos=None, out=print) -> Result | None:
    out(f"Checking {host}. Nothing will be sent.\n")
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror:
        out(f"  {host}: the host name could not be found. Check [smtp] host for typos.")
        return None
    results = []
    for port, security in combos or COMBOS:
        out(f"  port {port:<5} {security:<9} ... ", end="")
        res = try_one(host, port, security, username, password, timeout)
        results.append(res)
        mark = "OK  " if res.logged_in else ("ok  " if res.usable else "no  ")
        out(f"{mark}{res.message}")

    best = next((r for r in results if r.logged_in), None)
    if best is None:
        best = next((r for r in results if r.usable and r.security != "none"), None)
    if best is None and not username:
        # no password to protect: an unencrypted port is acceptable if it's all there is
        best = next((r for r in results if r.usable), None)
    out("")
    if best:
        out("Use these settings in the [smtp] section:")
        out(f"  port = {best.port}")
        out(f"  security = {best.security}")
        if not best.logged_in:
            out("(Login was not confirmed. Set username and password and run smtp-check again.)")
    elif any(r.logged_in is False for r in results):
        out("The server answered, but the login was refused. Check the username and password.")
    else:
        if all("timed out" in r.message for r in results):
            out("Every port timed out. That almost always means a firewall or antivirus on this computer, "
                "or the network you're on, is blocking outgoing mail. Try another network (a phone hotspot "
                "is a quick test) or allow Python through the firewall/antivirus.")
        else:
            out("No working setting found. Check the host name with your email provider.")
    return best
