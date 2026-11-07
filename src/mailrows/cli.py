"""Command line: mailrows send | smtp-check | resolve | init."""

from __future__ import annotations

import argparse
import shutil
import smtplib
from pathlib import Path

from mailrows import __version__, run, smtpcheck
from mailrows.config import ConfigError, load

STARTER_FILES = {
    "config.example.ini": "config.ini",
    "examples/events.csv": "events.csv",
    "examples/template.txt": "template.txt",
    "examples/template.html": "template.html",
}


def starter_source() -> Path:
    """Example files: inside the installed package, or the repository root when run from source."""
    pkg_dir = Path(__file__).resolve().parent
    packaged = pkg_dir / "_starter"
    return packaged if packaged.is_dir() else pkg_dir.parents[1]


def cmd_init(args) -> int:
    dest = Path(args.folder)
    dest.mkdir(parents=True, exist_ok=True)
    src = starter_source()
    made = []
    for rel, name in STARTER_FILES.items():
        target = dest / name
        if target.exists():
            print(f"  kept     {target} (already exists)")
            continue
        shutil.copyfile(src / rel, target)
        made.append(target)
        print(f"  created  {target}")
    gitignore = dest / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("config.ini\n*.sqlite3\n*.sqlite3-journal\n", encoding="utf-8")
        print(f"  created  {gitignore} (keeps config.ini and the send log out of git)")
    print("\nNext: open config.ini, fill in the [smtp] and [sender] sections, then run")
    print(f"  mailrows smtp-check -c {dest / 'config.ini'}")
    print(f"  mailrows send --dry-run -c {dest / 'config.ini'}")
    return 0


def cmd_send(args) -> int:
    settings = load(args.config)
    if args.dry_run:
        mode = run.DRY
    elif args.test_to:
        mode = run.TEST
    else:
        mode = run.REAL
    return run.send_command(settings, mode, test_to=args.test_to or "")


def cmd_smtp_check(args) -> int:
    host, user, password = args.host, args.username, None
    if args.config and Path(args.config).exists():
        s = load(args.config)
        host = host or s.get("smtp", "host", "")
        user = user if user is not None else s.get("smtp", "username", "")
        password = s.get("smtp", "password", "")
    if not host:
        raise ConfigError("Give a host: --host smtp.example.com, or set [smtp] host in the config file.")
    user = user or ""
    if user and (password is None or password.strip() in ("", "CHANGE_ME")):
        import getpass

        password = getpass.getpass(f"Password for {user} (not shown, not saved): ")
    best = smtpcheck.check(host, user, password or "", timeout=args.timeout)
    return 0 if best else 1


def cmd_resolve(args) -> int:
    return run.resolve_command(load(args.config), args.row_id, was_sent=args.sent)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailrows",
                                description="Send one templated email per spreadsheet row, safely.")
    p.add_argument("--version", action="version", version=f"mailrows {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def with_config(sp):
        sp.add_argument("-c", "--config", default="config.ini", help="settings file (default: config.ini)")

    s = sub.add_parser("send", help="check every row, show a preview, then send after you type yes")
    with_config(s)
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="render and check everything, send and change nothing")
    mode.add_argument("--test-to", metavar="ADDRESS",
                      help="send every email to ADDRESS with [TEST] in the subject; no files or logs change")
    s.set_defaults(func=cmd_send)

    c = sub.add_parser("smtp-check", help="find the port and security setting that work, without sending")
    with_config(c)
    c.add_argument("--host", help="mail server (default: [smtp] host from the config)")
    c.add_argument("--username", help="login to test (default: [smtp] username)")
    c.add_argument("--timeout", type=float, default=10, help="seconds to wait per attempt (default 10)")
    c.set_defaults(func=cmd_smtp_check)

    r = sub.add_parser("resolve", help="after checking your Sent folder, record whether an uncertain email went")
    with_config(r)
    r.add_argument("row_id")
    which = r.add_mutually_exclusive_group(required=True)
    which.add_argument("--sent", action="store_true", help="it arrived: never send it again")
    which.add_argument("--not-sent", action="store_true", help="it did not go: send it on the next run")
    r.set_defaults(func=cmd_resolve)

    i = sub.add_parser("init", help="create a starter folder with a config, a sample sheet and templates")
    i.add_argument("folder", nargs="?", default=".")
    i.set_defaults(func=cmd_init)
    return p


def main(argv=None) -> int:
    run.setup_stdout()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, run.SetupError, smtplib.SMTPException, OSError, KeyboardInterrupt) as exc:
        message, code = explain_stop(exc)
        print("\n" + message)
        return code


def explain_stop(exc: BaseException) -> tuple[str, int]:
    """A readable line, and the exit code, for whatever ended the run early."""
    if isinstance(exc, (ConfigError, run.SetupError)):
        return f"Stopped before sending anything: {exc}", run.EXIT_SETUP
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The mail server refused the username or password. Nothing was sent. "
                "Run 'mailrows smtp-check' to test the login."), run.EXIT_SETUP
    if isinstance(exc, KeyboardInterrupt):
        return "Stopped. Anything already sent is in the send log and won't be sent again.", 130
    return (f"Stopped: {exc}. Every email already sent is in the send log and won't be sent again. "
            "If this is a mail server problem, 'mailrows smtp-check' can help."), run.EXIT_SETUP
