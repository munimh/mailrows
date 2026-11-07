import os
import subprocess
import sys
import textwrap

from conftest import ROWS, SRC

from mailrows import run
from mailrows.ledger import Ledger


def test_real_send_updates_status_and_log(project, smtp):
    assert project.run() == run.EXIT_OK
    msgs = smtp.parsed()
    assert [m["To"] for m in msgs] == ["alex@example.com", "blair@example.com", "casey@example.com"]
    assert msgs[0]["Subject"] == "News about Gardening"
    assert msgs[0]["From"] == "Example Club <club@example.com>"
    assert msgs[0]["Reply-To"] == "reply@example.com"
    assert "Hello Alex Example," in msgs[0].get_content()
    assert project.status() == {"A1": "sent", "A2": "sent", "A3": "sent"}
    log = Ledger(project.log)
    assert {e.state for e in log.all_entries().values()} == {"sent"}
    log.close()


def test_preview_and_sample_shown_before_asking(project, smtp):
    project.run(answer="no")
    out = project.output
    assert "Will be emailed" in out and "alex@example.com" in out
    assert "Subject:  News about Gardening" in out
    assert "Hello Alex Example," in out
    assert project.asked and "Type yes" in project.asked[0]


def test_anything_but_yes_cancels(project, smtp):
    before = project.snapshot()
    for answer in ("no", "y", "YES please", ""):
        assert project.run(answer=answer) == run.EXIT_CANCELLED
    assert smtp.messages == []
    assert project.snapshot() == before


def test_rerun_sends_nothing(project, smtp):
    project.run()
    assert len(smtp.messages) == 3
    assert project.run() == run.EXIT_OK
    assert len(smtp.messages) == 3
    assert "Nothing to send" in project.output
    assert project.asked == []  # nothing to confirm


def test_rerun_after_status_cleared_and_sheet_edited(project, smtp):
    project.run()
    edited = [[r[0], r[1] + " Jr", r[2], "Something new", ""] for r in ROWS]  # status cleared, text changed
    project.write_rows(edited)
    project.run()
    assert len(smtp.messages) == 3
    assert "won't be sent again" in project.output


def test_same_email_under_a_new_id_is_not_sent_again(project, smtp):
    project.run()
    project.write_rows(ROWS + [["A9", *ROWS[0][1:4], ""]])  # a copy of A1 with a new ID
    project.run()
    assert len(smtp.messages) == 3
    assert "already went to this address as ID A1" in project.output


def test_new_rows_added_later_are_sent(project, smtp):
    project.run()
    project.write_rows([r[:4] + ["sent"] for r in ROWS] + [["A4", "Drew Example", "drew@example.com", "Chess", ""]])
    project.run()
    assert [m["To"] for m in smtp.parsed()][-1] == "drew@example.com"
    assert len(smtp.messages) == 4


def test_bad_rows_are_listed_and_never_sent(project, smtp):
    project.write_rows([
        ["B1", "Good Example", "good@example.com", "Chess", ""],
        ["B2", "No At Sign", "not-an-address", "Chess", ""],
        ["B3", "", "noname@example.com", "Chess", ""],          # required column empty
        ["B4", "Twin One", "twin1@example.com", "Chess", ""],
        ["B4", "Twin Two", "twin2@example.com", "Chess", ""],   # duplicate ID: neither is sent
        ["", "No Id", "noid@example.com", "Chess", ""],
        ["B6", "Bad Domain", "someone@example", "Chess", ""],
    ])
    assert project.run() == run.EXIT_PROBLEMS
    assert [to for _, (to,), _ in smtp.messages] == ["good@example.com"]
    out = project.output
    assert "Can't be sent (6)" in out
    assert "'not-an-address' is not a valid email address" in out
    assert "empty: name" in out
    assert "IDs must be unique" in out
    assert project.status()["B1"] == "sent"
    assert project.status()["B2"] == ""


def test_unknown_placeholder_stops_before_anything(project, smtp):
    import pytest

    (project.folder / "template.txt").write_text("Subject: Hi {name}\nYour {colour} badge\n", encoding="utf-8")
    before = project.snapshot()
    with pytest.raises(run.SetupError, match=r"\{colour\}"):
        project.run()
    assert smtp.messages == []
    assert project.snapshot() == before


def test_dry_run_changes_nothing(project, smtp):
    before = project.snapshot()
    assert project.run(mode=run.DRY) == run.EXIT_OK
    assert smtp.messages == []
    assert project.snapshot() == before
    assert not project.log.exists()
    assert "Dry run: 3 email(s) would be sent" in project.output
    assert project.asked == []


def test_dry_run_after_real_run_changes_nothing(project, smtp):
    project.run()
    before = project.snapshot()
    project.run(mode=run.DRY)
    assert project.snapshot() == before
    assert len(smtp.messages) == 3


def test_test_mode_sends_to_one_address_and_changes_nothing(project, smtp):
    project.write_config(sender={"bcc": "archive@example.com"})
    before = project.snapshot()
    assert project.run(mode=run.TEST, test_to="me@example.com") == run.EXIT_OK
    msgs = smtp.parsed()
    assert len(msgs) == 3
    assert {tuple(rcpts) for _, rcpts, _ in smtp.messages} == {("me@example.com",)}  # no bcc on tests
    assert all(m["Subject"].startswith("[TEST] News about") for m in msgs)
    assert all(m["To"] == "me@example.com" for m in msgs)
    assert project.snapshot() == before
    assert not project.log.exists()


def test_test_mode_after_real_run_still_changes_nothing(project, smtp):
    project.run()
    before = project.snapshot()
    project.run(mode=run.TEST, test_to="me@example.com")
    assert project.snapshot() == before
    assert len(smtp.messages) == 3  # already-sent rows are not part of a test run


def test_bcc_goes_on_envelope_only(project, smtp):
    project.write_config(sender={"bcc": "archive@example.com"})
    project.run()
    for (_, rcpts, raw), msg in zip(smtp.messages, smtp.parsed()):
        assert rcpts[1] == "archive@example.com"
        assert "Bcc" not in msg and b"archive@example.com" not in raw


def test_refused_recipient_is_retried_next_run(project, smtp):
    smtp.refuse = {"blair@example.com"}
    assert project.run() == run.EXIT_PROBLEMS
    assert len(smtp.messages) == 2
    assert project.status()["A2"] == ""
    smtp.refuse = set()
    project.run()
    assert [to for _, (to,), _ in smtp.messages] == ["alex@example.com", "casey@example.com", "blair@example.com"]


def test_connection_dropped_after_data_is_never_resent(project, smtp):
    smtp.drop_after_data = 1  # the first email reaches the server, then the line goes dead
    assert project.run() == run.EXIT_PROBLEMS
    assert project.status()["A1"] == "check: may have been sent"
    assert len(smtp.messages) == 3  # the other two still went (after a reconnect)
    project.run()
    assert len(smtp.messages) == 3
    assert "may have been sent" in project.output
    # a person checks the Sent folder: it did not arrive, so allow it again
    assert run.resolve_command(project.settings(), "A1", was_sent=False, out=lambda *a: None) == 0
    assert project.status()["A1"] == ""
    project.run()
    assert len(smtp.messages) == 4


def test_crash_between_sending_and_logging_never_double_sends(project, smtp, tmp_path):
    """Kill the process right after the server accepted the first email, before the log says 'sent'."""
    script = tmp_path / "crash.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(SRC)!r})
        from mailrows import ledger, cli
        ledger.Ledger.mark_sent = lambda self, row_id: os._exit(9)
        sys.exit(cli.main(["send", "-c", {str(project.config)!r}]))
    """), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAILROWS_")}
    proc = subprocess.run([sys.executable, str(script)], input="yes\n", text=True, capture_output=True,
                          timeout=60, env=env)
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert len(smtp.messages) == 1  # the email went, and then the process died

    project.run()  # the rerun must not send A1 again, but sends the two that never went
    assert [to for _, (to,), _ in smtp.messages] == ["alex@example.com", "blair@example.com", "casey@example.com"]
    assert "may have been sent" in project.output
    assert project.status()["A1"] == "check: may have been sent"

    project.run()
    assert len(smtp.messages) == 3

    run.resolve_command(project.settings(), "A1", was_sent=True, out=lambda *a: None)
    project.run()
    assert len(smtp.messages) == 3
    assert project.status()["A1"] == "sent"


def test_locked_status_file_does_not_cause_a_second_send(project, smtp, monkeypatch):
    from mailrows.table import CsvTable, FileLocked

    real = CsvTable.set_values

    def locked(self, *a, **k):
        raise FileLocked("people.csv is open in another program")

    monkeypatch.setattr(CsvTable, "set_values", locked)
    project.run()
    assert len(smtp.messages) == 3
    assert "couldn't update the status column" in project.output
    monkeypatch.setattr(CsvTable, "set_values", real)
    project.write_rows(ROWS)  # status column still empty
    project.run()
    assert len(smtp.messages) == 3


def test_xlsx_input_keeps_formulas_and_writes_status(tmp_path, smtp):
    import openpyxl

    from conftest import HEADERS, Project, TEMPLATE

    p = Project(tmp_path, smtp.port)
    p.sheet = tmp_path / "people.xlsx"
    p.write_rows(ROWS, headers=HEADERS + ["count"])
    wb = openpyxl.load_workbook(p.sheet)
    wb.active["F2"] = "=LEN(B2)"
    wb.save(p.sheet)
    (tmp_path / "template.txt").write_text(TEMPLATE, encoding="utf-8")
    p.write_config()
    assert p.run() == run.EXIT_OK
    assert len(smtp.messages) == 3
    wb = openpyxl.load_workbook(p.sheet)
    assert wb.active["F2"].value == "=LEN(B2)"
    assert [wb.active.cell(row=r, column=5).value for r in (2, 3, 4)] == ["sent"] * 3


def test_html_version_is_escaped(project, smtp):
    (project.folder / "body.html").write_text("<p>Hello {name}, about {topic}</p>", encoding="utf-8")
    project.write_rows([["H1", "Sam <b>Bold</b> & Co", "sam@example.com", "Art", ""]])
    project.write_config(template={"html": "body.html"})
    project.run()
    msg = smtp.parsed()[0]
    html = msg.get_body(("html",)).get_content()
    assert "Sam &lt;b&gt;Bold&lt;/b&gt; &amp; Co" in html
    assert "Hello Sam <b>Bold</b> & Co," in msg.get_body(("plain",)).get_content()


def test_non_ascii_text_survives(project, smtp):
    project.write_rows([["U1", "Zoë Ångström", "zoe@example.com", "Café crème", ""]])
    project.run()
    msg = smtp.parsed()[0]
    assert msg["Subject"] == "News about Café crème"
    assert "Hello Zoë Ångström," in msg.get_content()
