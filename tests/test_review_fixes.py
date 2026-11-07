"""Tests for the follow-ups from the first code review."""

import io
import smtplib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ROWS
from test_hooks import airtable_config, seed

from mailrows import cli, config, run
from mailrows.hooks.http import HttpError, JsonClient
from mailrows.ledger import Ledger
from mailrows.mailer import Mailer
from mailrows.table import CsvTable


# == placeholders vs real secrets ==

@pytest.mark.parametrize("value", ["Tr0ub4dor<3", "my_your_dog", "<3cats", "abc>", "xxxxxxxx1"])
def test_real_secrets_with_odd_characters_are_accepted(value):
    assert config.looks_unset(value) is False


@pytest.mark.parametrize("value", ["", "  ", "CHANGE_ME", "change_me", "<paste your token here>"])
def test_placeholders_count_as_unset(value):
    assert config.looks_unset(value) is True


# == no redirects with credentials ==

class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append(self.headers.get("Authorization"))
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")


def _server(redirect_to=""):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    srv.seen, srv.redirect_to = [], redirect_to
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_redirects_are_refused_and_credentials_stay_put():
    elsewhere = _server()
    first = _server(redirect_to=f"http://127.0.0.1:{elsewhere.server_address[1]}/steal")
    try:
        client = JsonClient({"Authorization": "Bearer secret"}, retries=0)
        with pytest.raises(HttpError, match="302"):
            client.call("GET", f"http://127.0.0.1:{first.server_address[1]}/v0/x")
        assert first.seen == ["Bearer secret"]
        assert elsewhere.seen == []  # the token never reached the other server
    finally:
        for s in (first, elsewhere):
            s.shutdown()
            s.server_close()


def test_webhook_secret_over_plain_http_is_refused(project):
    project.write_config(hooks="webhook", webhook={"url": "http://hooks.example.com/x",
                                                   "header_name": "X-Token", "header_value": "s3cret"})
    with pytest.raises(run.SetupError, match="unencrypted"):
        project.run()


# == Ctrl+C ==

def test_ctrl_c_before_the_email_goes_releases_the_row(project, smtp, monkeypatch):
    def interrupted(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(smtplib.SMTP, "rcpt", interrupted)
    with pytest.raises(KeyboardInterrupt):
        project.run()
    assert smtp.messages == []
    log = Ledger(project.log)
    assert log.all_entries() == {}  # nothing held as "may have been sent"
    log.close()
    monkeypatch.undo()
    project.run()
    assert len(smtp.messages) == 3


def test_ctrl_c_while_the_email_is_going_keeps_it_held(project, smtp, monkeypatch):
    def interrupted(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(smtplib.SMTP, "data", interrupted)
    with pytest.raises(KeyboardInterrupt):
        project.run()
    log = Ledger(project.log)
    assert log.entry("A1").state == "sending"
    log.close()
    monkeypatch.undo()
    project.run()
    assert "may have been sent" in project.output
    assert [to for _, (to,), _ in smtp.messages] == ["blair@example.com", "casey@example.com"]


def test_cli_reports_ctrl_c_without_a_traceback(project, smtp, monkeypatch, capsys):
    monkeypatch.setattr(smtplib.SMTP, "rcpt", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    assert cli.main(["send", "-c", str(project.config)]) == 130
    assert "Stopped." in capsys.readouterr().out


# == mail server problems ==

def test_login_failure_stops_before_any_email(project, smtp, monkeypatch, capsys):
    smtp.password_ok = False
    project.write_config(smtp={"username": "club@example.com", "password": "wrong-password"})
    monkeypatch.setattr("sys.stdin", io.StringIO("yes\n"))
    assert cli.main(["send", "-c", str(project.config)]) == run.EXIT_SETUP
    assert "refused the username or password" in capsys.readouterr().out
    assert smtp.messages == []
    log = Ledger(project.log)
    assert log.all_entries() == {}
    log.close()
    assert project.status() == {"A1": "", "A2": "", "A3": ""}


def test_unreachable_server_stops_after_two_tries(project, smtp, monkeypatch):
    project.write_rows(ROWS + [[f"Z{i}", f"Zed {i}", f"z{i}@example.com", "Chess", ""] for i in range(3)])
    calls = []

    def down(self):
        calls.append(1)
        raise OSError("connection timed out")

    monkeypatch.setattr(Mailer, "_ready", down)
    assert project.run() == run.EXIT_PROBLEMS
    assert len(calls) == 2
    assert "could not be reached 2 times in a row" in project.output
    assert "4 email(s) were not tried" in project.output
    monkeypatch.undo()
    project.run()
    assert len(smtp.messages) == 6


def test_test_to_must_be_an_email_address(project, smtp):
    with pytest.raises(run.SetupError, match="--test-to"):
        project.run(mode=run.TEST, test_to="not-an-address")
    assert smtp.messages == []


# == uncertain send + Airtable ==

def test_uncertain_send_resolved_as_sent_runs_only_the_update(project, smtp, airtable):
    seed(airtable)
    airtable_config(project, airtable)
    smtp.drop_after_data = 1
    project.run()
    assert len(smtp.messages) == 3
    assert [rid for rid, _ in airtable.patches] == ["rec002", "rec003"]
    assert project.status()["A1"] == "check: may have been sent"

    run.resolve_command(project.settings(), "A1", was_sent=True, out=lambda *a: None)
    assert project.status()["A1"] == "sent, update pending"
    assert project.run() == run.EXIT_OK
    assert len(smtp.messages) == 3  # no second email
    assert [rid for rid, _ in airtable.patches] == ["rec002", "rec003", "rec001"]
    assert project.status()["A1"] == "sent"


# == spreadsheet writes ==

@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_csv_keeps_its_line_endings(project, smtp, ending):
    lines = [b"id,name,email,topic,status"] + [",".join(r).encode() for r in ROWS]
    project.sheet.write_bytes(ending.join(lines) + ending)
    project.run()
    data = project.sheet.read_bytes()
    assert data.count(ending) == 4
    if ending == b"\n":
        assert b"\r\n" not in data
    assert project.status() == {"A1": "sent", "A2": "sent", "A3": "sent"}


@pytest.mark.parametrize("every,saves", [(1, 3), (2, 2), (10, 1)])
def test_status_saves_can_be_grouped(project, smtp, monkeypatch, every, saves):
    project.write_config(input={"status_save_every": str(every)})
    real, count = CsvTable.set_values, []

    def counting(self, *a, **k):
        count.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(CsvTable, "set_values", counting)
    project.run()
    assert len(count) == saves
    assert project.status() == {"A1": "sent", "A2": "sent", "A3": "sent"}


# == readable errors ==

def test_unusable_send_log_gives_a_readable_message(project, smtp, capsys):
    (project.folder / "logdir").mkdir()
    project.write_config(log={"path": "logdir"})
    assert cli.main(["send", "--dry-run", "-c", str(project.config)]) == run.EXIT_SETUP
    assert "send log couldn't be used" in capsys.readouterr().out
