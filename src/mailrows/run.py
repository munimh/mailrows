"""Checking the rows, showing the preview, and sending."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mailrows import ledger as ledger_mod
from mailrows.config import ConfigError, Settings
from mailrows.hooks import Hook, HookError, HookRow, load_hooks
from mailrows.mailer import Mailer, Refused, Sender, SmtpSettings, Uncertain, build_message, pause
from mailrows.table import FileLocked, Row, Table, TableError
from mailrows.template import Rendered, Template, TemplateError

EMAIL = re.compile(r"^[^@\s<>()\[\],;:\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")

REAL, DRY, TEST = "real", "dry-run", "test"

EXIT_OK, EXIT_PROBLEMS, EXIT_SETUP, EXIT_CANCELLED = 0, 1, 2, 3


class SetupError(Exception):
    """Something to fix before anything can be sent. Nothing has been sent."""


@dataclass
class Item:
    row: Row
    row_id: str
    recipient: str = ""
    action: str = "send"  # send | hooks | skip | bad
    reason: str = ""
    rendered: Rendered | None = None
    hooks: list[str] = field(default_factory=list)  # hooks still to run, for action "hooks"
    hook_row: HookRow | None = None
    sent_at: str = ""


@dataclass
class Job:
    settings: Settings
    table: Table
    columns: list[str]
    template: Template
    id_col: str
    email_col: str
    status_col: str
    status_sent: str
    status_pending: str
    status_unknown: str
    log_path: Path
    hooks: list[Hook]
    items: list[Item]

    def of(self, action: str) -> list[Item]:
        return [i for i in self.items if i.action == action]


def valid_email(addr: str) -> bool:
    return bool(EMAIL.match(addr)) and len(addr) <= 254 and ".." not in addr


def _out(*parts, end="\n"):
    print(*parts, end=end, flush=True)


# ---------------------------------------------------------------- planning

def prepare(settings: Settings, mode: str, out=_out) -> Job:
    try:
        table = Table.open(settings.resolve(settings.get("input", "file")),
                           sheet=settings.get("input", "sheet", ""), delimiter=settings.get("input", "delimiter", ""))
        columns, rows = table.read()
        html = settings.get("template", "html", "")
        template = Template.load(settings.resolve(settings.get("template", "text")),
                                 settings.resolve(html) if html else None)
    except (TableError, TemplateError, ConfigError) as exc:
        raise SetupError(str(exc)) from None

    id_col = settings.get("input", "id_column")
    email_col = settings.get("input", "email_column")
    status_col = settings.get("input", "status_column", "")
    required = settings.get_list("input", "required_columns")
    for label, col in (("id_column", id_col), ("email_column", email_col), ("status_column", status_col)):
        if col and col not in columns:
            raise SetupError(f"[input] {label} is '{col}', but the spreadsheet has no such column. "
                             f"Columns are: {', '.join(columns)}.")
    missing = [c for c in required if c not in columns]
    if missing:
        raise SetupError(f"[input] required_columns lists {', '.join(missing)}, not in the spreadsheet.")
    try:
        template.check_columns(columns)
        hooks = load_hooks(settings) if mode != TEST else []
        for h in hooks:
            h.check_columns(columns)
    except (TemplateError, ConfigError) as exc:
        raise SetupError(str(exc)) from None

    job = Job(settings=settings, table=table, columns=columns, template=template, id_col=id_col,
              email_col=email_col, status_col=status_col,
              status_sent=settings.get("status", "sent", "sent"),
              status_pending=settings.get("status", "pending", "sent, update pending"),
              status_unknown=settings.get("status", "unknown", "check: may have been sent"),
              log_path=settings.resolve(settings.get("log", "path", "mailrows-log.sqlite3")),
              hooks=hooks, items=[])
    log = ledger_mod.Ledger.read_only(job.log_path)
    try:
        job.items = _classify(job, rows, required, log, mode)
    finally:
        log.close()
    return job


def _classify(job: Job, rows: list[Row], required: list[str], log, mode: str) -> list[Item]:
    counts: dict[str, int] = {}
    for r in rows:
        rid = r.values.get(job.id_col, "").strip()
        counts[rid] = counts.get(rid, 0) + 1
    enabled = [h.name for h in job.hooks]
    markers = {m.lower() for m in (job.status_sent, job.status_pending, job.status_unknown)}
    seen_fp: dict[str, str] = {}
    items = []
    for r in rows:
        rid = r.values.get(job.id_col, "").strip()
        it = Item(row=r, row_id=rid, recipient=r.values.get(job.email_col, "").strip())
        items.append(it)
        if not rid:
            it.action, it.reason = "bad", f"'{job.id_col}' is empty"
            continue
        if counts[rid] > 1:
            others = [str(o.line) for o in rows if o.values.get(job.id_col, "").strip() == rid and o is not r]
            it.action, it.reason = "bad", f"ID {rid} is also on line {', '.join(others)}. IDs must be unique"
            continue

        entry = log.entry(rid)
        if entry is not None:
            it.sent_at = entry.sent_at or ""
            if entry.state == "sending":
                it.action = "bad"
                it.reason = (f"may have been sent: the run stopped while it was going. Check your Sent folder, "
                             f"then run 'mailrows resolve {rid} --sent' or '--not-sent'")
                continue
            pending = entry.pending_hooks
            if pending and mode != TEST:
                it.action, it.hooks = "hooks", pending
                gone = [h for h in pending if h not in enabled]
                if gone:
                    it.action = "bad"
                    it.reason = (f"the email went out, but the {', '.join(gone)} update is pending and that hook is not "
                                 "enabled under [hooks]")
                continue
            it.action = "skip"
            it.reason = f"already sent {entry.sent_at[:16].replace('T', ' ') if entry.sent_at else ''}".strip()
            if pending:
                it.reason += f" ({', '.join(pending)} update pending)"
            try:
                if job.template.render(entry.recipient, r.values).fingerprint() != entry.fingerprint:
                    it.reason += "; the row has changed since, but it won't be sent again"
            except KeyError:
                pass
            continue

        status = r.values.get(job.status_col, "").strip().lower() if job.status_col else ""
        if status and status in markers:
            if status == job.status_unknown.lower():
                it.action, it.reason = "bad", (f"status says '{job.status_unknown}'. Check your Sent folder, "
                                               "then clear the status if it did not go")
            else:
                it.action, it.reason = "skip", f"status column already says '{r.values.get(job.status_col)}'"
            continue

        empty = [c for c in required if not r.values.get(c, "").strip()]
        if empty:
            it.action, it.reason = "bad", f"empty: {', '.join(empty)}"
            continue
        if not valid_email(it.recipient):
            it.action, it.reason = "bad", f"'{it.recipient}' is not a valid email address" if it.recipient \
                else f"'{job.email_col}' is empty"
            continue

        it.rendered = job.template.render(it.recipient, r.values)
        fp = it.rendered.fingerprint()
        earlier = log.by_fingerprint(fp)
        if earlier:
            it.action, it.reason = "skip", f"exactly this email already went to this address as ID {earlier}"
            continue
        if fp in seen_fp:
            it.action, it.reason = "bad", f"exactly the same email as ID {seen_fp[fp]}, to the same address"
            continue
        seen_fp[fp] = rid
        it.hooks = list(enabled)
    return items


def hook_rows(job: Job) -> None:
    """Run every hook's pre-check for the rows that will need it. Rows that fail are taken out."""
    work = [i for i in job.items if i.action in ("send", "hooks")]
    if not work or not job.hooks:
        return
    for it in work:
        subject = it.rendered.subject if it.rendered else ""
        it.hook_row = HookRow(row_id=it.row_id, values=dict(it.row.values), recipient=it.recipient,
                              subject=subject, sent_at=it.sent_at)
    for hook in job.hooks:
        needs = [i for i in work if hook.name in i.hooks and i.action in ("send", "hooks")]
        if not needs:
            continue
        try:
            problems = hook.prepare([i.hook_row for i in needs])
        except HookError as exc:
            raise SetupError(f"{exc}. Nothing has been sent.") from None
        for it in needs:
            why = problems.get(it.row_id)
            if not why:
                continue
            if it.action == "send":
                it.action, it.reason = "bad", why
            else:
                it.action, it.reason = "bad", f"the email went out and its update is pending, but {why}"


# ---------------------------------------------------------------- showing

def _cut(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "~"


def show(job: Job, mode: str, test_to: str = "", out=_out) -> None:
    send, upd, bad, skip = job.of("send"), job.of("hooks"), job.of("bad"), job.of("skip")
    rows = send + upd
    if rows:
        title = "Will be emailed" if mode != TEST else f"Test emails (all go to {test_to})"
        out(f"\n{title}:\n")
        w_id = max(4, min(16, max(len(i.row_id) for i in rows)))
        out(f"  {'Line':>5}  {'ID':<{w_id}}  {'To':<34}  What happens")
        out("  " + "-" * (5 + 2 + w_id + 2 + 34 + 2 + 30))
        for it in rows:
            if it.action == "hooks":
                what = f"retry {', '.join(it.hooks)} update only (email already sent)"
            elif mode == TEST:
                what = "test email"
            else:
                what = "send" + (f" + {', '.join(it.hooks)}" if it.hooks else "")
            out(f"  {it.row.line:>5}  {_cut(it.row_id, w_id):<{w_id}}  {_cut(it.recipient, 34):<34}  {what}")
    if bad:
        out(f"\nCan't be sent ({len(bad)}), skipped:\n")
        for it in bad:
            out(f"  line {it.row.line:<5} {_cut(it.row_id or '-', 16):<16} {it.reason}")
    if skip:
        out(f"\nAlready done ({len(skip)}), skipped:")
        for it in skip[:10]:
            out(f"  line {it.row.line:<5} {_cut(it.row_id, 16):<16} {it.reason}")
        if len(skip) > 10:
            out(f"  ... and {len(skip) - 10} more")
    if send:
        sample(job, send[0], mode, test_to, out)


def sample(job: Job, it: Item, mode: str, test_to: str, out=_out) -> None:
    sender = Sender.from_settings(job.settings)
    r = it.rendered
    out(f"\nSample email (line {it.row.line}, ID {it.row_id}):")
    out("  " + "=" * 70)
    frm = f"{sender.from_name} <{sender.from_email}>" if sender.from_name else sender.from_email
    out(f"  From:     {frm}")
    out(f"  To:       {test_to if mode == TEST else r.to}")
    if sender.reply_to:
        out(f"  Reply-To: {sender.reply_to}")
    if sender.bcc and mode != TEST:
        out(f"  Bcc:      {sender.bcc}")
    out(f"  Subject:  {'[TEST] ' if mode == TEST else ''}{r.subject}")
    out("  " + "-" * 70)
    for line in r.text.rstrip("\n").split("\n"):
        out(f"  {line}")
    out("  " + "=" * 70)
    if r.html is not None:
        out(f"  (also has an HTML version, {len(r.html)} characters)")


# ---------------------------------------------------------------- sending

class Statuses:
    """Writes the status column. If the file is locked, warns once and carries on: the send log,
    not the spreadsheet, is what prevents a second email."""

    def __init__(self, job: Job, out):
        self.job, self.out, self.failed = job, out, False

    def set(self, updates: dict[str, str]) -> None:
        if not self.job.status_col or not updates:
            return
        try:
            self.job.table.set_values(self.job.id_col, self.job.status_col, updates)
        except (FileLocked, TableError, OSError) as exc:
            if not self.failed:
                self.out(f"  ! couldn't update the status column ({exc}). Carrying on: the send log "
                         "already records every email, and the next run fixes the column.")
            self.failed = True

    def sync(self, log: ledger_mod.Ledger) -> None:
        """Make the status column agree with the send log."""
        if not self.job.status_col:
            return
        _, rows = self.job.table.read()
        entries = log.all_entries()
        updates = {}
        for r in rows:
            rid = r.values.get(self.job.id_col, "").strip()
            e = entries.get(rid)
            if e is None:
                continue
            want = (self.job.status_unknown if e.state == "sending"
                    else self.job.status_pending if e.pending_hooks else self.job.status_sent)
            if r.values.get(self.job.status_col, "") != want:
                updates[rid] = want
        self.set(updates)


def run_hooks(job: Job, it: Item, log: ledger_mod.Ledger, out) -> bool:
    ok = True
    by_name = {h.name: h for h in job.hooks}
    it.hook_row.sent_at = it.sent_at
    for name in it.hooks:
        try:
            by_name[name].run(it.hook_row)
        except Exception as exc:  # any failure leaves the update pending for the next run
            ok = False
            log.note(it.row_id, f"hook {name} failed", str(exc))
            out(f"      {name} update failed: {exc}. It will be retried on the next run; "
                "the email will not be sent again.")
            continue
        log.mark_hook_done(it.row_id, name)
    return ok


def execute(job: Job, mode: str, test_to: str = "", out=_out) -> int:
    """Send. The caller has already shown the preview and had 'yes' typed."""
    if mode == TEST:
        return _execute_test(job, test_to, out)

    log = ledger_mod.Ledger(job.log_path)
    status = Statuses(job, out)
    problems = len(job.of("bad"))
    sent = updated = 0
    try:
        status.sync(log)
        send, upd = job.of("send"), job.of("hooks")
        for it in upd:
            if run_hooks(job, it, log, out):
                updated += 1
                status.set({it.row_id: job.status_sent})
                out(f"  updated   {it.row_id}")
            else:
                problems += 1
        if send:
            smtp, sender = SmtpSettings.from_settings(job.settings), Sender.from_settings(job.settings)
            delay = job.settings.get_float("sender", "delay_seconds", 2)
            with Mailer(smtp, sender) as mailer:
                for n, it in enumerate(send):
                    if n:
                        pause(delay)
                    fp = it.rendered.fingerprint()
                    try:
                        log.claim(it.row_id, fp, it.recipient, it.hooks)
                    except ledger_mod.ClaimTaken:
                        out(f"  skipped   {it.row_id}: another run has already sent it")
                        continue
                    message = build_message(sender, it.rendered)
                    envelope = [it.recipient] + ([sender.bcc] if sender.bcc else [])
                    try:
                        mailer.send(message, envelope)
                    except Refused as exc:
                        log.release(it.row_id, str(exc))
                        problems += 1
                        out(f"  FAILED    {it.row_id} ({it.recipient}): {exc}. Not sent; a rerun will try again.")
                        continue
                    except Uncertain as exc:
                        log.note(it.row_id, "uncertain", str(exc))
                        status.set({it.row_id: job.status_unknown})
                        problems += 1
                        out(f"  UNKNOWN   {it.row_id} ({it.recipient}): {exc}. It won't be sent again "
                            f"automatically: check your Sent folder, then run 'mailrows resolve {it.row_id}'.")
                        continue
                    log.mark_sent(it.row_id)
                    sent += 1
                    it.sent_at = (log.entry(it.row_id).sent_at or "")
                    if it.hooks:
                        status.set({it.row_id: job.status_pending})
                        if run_hooks(job, it, log, out):
                            status.set({it.row_id: job.status_sent})
                            out(f"  sent      {it.row_id} -> {it.recipient} (+ {', '.join(it.hooks)})")
                        else:
                            problems += 1
                            out(f"  sent      {it.row_id} -> {it.recipient} (update pending)")
                    else:
                        status.set({it.row_id: job.status_sent})
                        out(f"  sent      {it.row_id} -> {it.recipient}")
        if status.failed:
            status.failed = False
            status.sync(log)
    finally:
        log.close()
    out(f"\nDone. Emailed: {sent}   Updates retried: {updated}   Problems: {problems}")
    if status.failed:
        out("The status column could not be fully updated. Close the spreadsheet and run again to fix it; "
            "nothing will be sent twice.")
    return EXIT_PROBLEMS if problems else EXIT_OK


def _execute_test(job: Job, test_to: str, out) -> int:
    smtp, sender = SmtpSettings.from_settings(job.settings), Sender.from_settings(job.settings)
    delay = job.settings.get_float("sender", "delay_seconds", 2)
    sent = failed = 0
    with Mailer(smtp, sender) as mailer:
        for n, it in enumerate(job.of("send")):
            if n:
                pause(delay)
            r = it.rendered
            test = Rendered(to=test_to, subject="[TEST] " + r.subject, text=r.text, html=r.html)
            try:
                mailer.send(build_message(sender, test), [test_to])
                sent += 1
                out(f"  test sent {it.row_id} (would go to {it.recipient})")
            except (Refused, Uncertain) as exc:
                failed += 1
                out(f"  FAILED    {it.row_id}: {exc}")
    out(f"\nTest emails sent to {test_to}: {sent}   Failed: {failed}. No files or logs were changed.")
    return EXIT_PROBLEMS if failed else EXIT_OK


# ---------------------------------------------------------------- the whole thing

def send_command(settings: Settings, mode: str, test_to: str = "", ask=input, out=_out) -> int:
    if mode == TEST and not valid_email(test_to):
        raise SetupError(f"--test-to needs an email address, not '{test_to}'.")
    job = prepare(settings, mode, out)
    if mode != TEST:
        hook_rows(job)
        for h in job.hooks:
            out(f"Hook: {h.describe()}")
    if mode == REAL and job.status_col and (job.of("send") or job.of("hooks")):
        try:
            job.table.check_writable()
        except FileLocked as exc:
            raise SetupError(str(exc)) from None
    show(job, mode, test_to, out)
    send, upd = job.of("send"), job.of("hooks")
    if mode == TEST:
        upd = []
    bad = len(job.of("bad"))
    if not send and not upd:
        out("\nNothing to send." + (" Fix the rows listed above and run again." if bad else ""))
        return EXIT_PROBLEMS if bad else EXIT_OK
    if mode == DRY:
        out(f"\nDry run: {len(send)} email(s) would be sent"
            + (f" and {len(upd)} pending update(s) retried" if upd else "")
            + ". Nothing was sent and no file was changed.")
        return EXIT_PROBLEMS if bad else EXIT_OK

    if mode == TEST:
        question = f"\nSend {len(send)} TEST email(s) to {test_to}? Type yes to continue: "
    else:
        bits = []
        if send:
            bits.append(f"send {len(send)} email(s)")
        if upd:
            bits.append(f"retry {len(upd)} pending update(s)")
        question = f"\n{' and '.join(bits).capitalize()}? Type yes to continue: "
    try:
        answer = ask(question)
    except EOFError:
        answer = ""
    if answer.strip().lower() != "yes":
        out("Cancelled. Nothing was sent.")
        return EXIT_CANCELLED
    return execute(job, mode, test_to, out)


def resolve_command(settings: Settings, row_id: str, was_sent: bool, out=_out) -> int:
    path = settings.resolve(settings.get("log", "path", "mailrows-log.sqlite3"))
    if not path.exists():
        raise SetupError(f"No send log at {path}.")
    log = ledger_mod.Ledger(path)
    try:
        before = log.entry(row_id)
        if before is None:
            out(f"ID {row_id} is not in the send log, so there is nothing to resolve.")
            return EXIT_PROBLEMS
        if before.state != "sending":
            out(f"ID {row_id} is already recorded as sent. Nothing changed.")
            return EXIT_OK
        log.resolve(row_id, was_sent)
    finally:
        log.close()
    status_col = settings.get("input", "status_column", "")
    if status_col:
        try:
            table = Table.open(settings.resolve(settings.get("input", "file")),
                               sheet=settings.get("input", "sheet", ""),
                               delimiter=settings.get("input", "delimiter", ""))
            sent_value = settings.get("status", "pending" if before.pending_hooks else "sent",
                                      "sent, update pending" if before.pending_hooks else "sent")
            table.set_values(settings.get("input", "id_column"), status_col, {row_id: sent_value if was_sent else ""})
        except (TableError, OSError) as exc:
            out(f"(The status column could not be updated: {exc}. Do it by hand, or the next run will.)")
    if was_sent:
        out(f"Recorded ID {row_id} as sent. It won't be sent again; pending updates run on the next run.")
    else:
        out(f"Recorded ID {row_id} as not sent. The next run will send it.")
    return EXIT_OK


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
