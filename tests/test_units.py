import socket

import pytest

from mailrows import cli, config, smtpcheck
from mailrows.run import valid_email
from mailrows.template import Template, TemplateError


def make_template(tmp_path, text):
    p = tmp_path / "t.txt"
    p.write_text(text, encoding="utf-8")
    return Template.load(p)


def test_template_subject_and_body(tmp_path):
    t = make_template(tmp_path, "Subject: Hi {first name}\nDear {first name},\n{{not a field}}\n")
    r = t.render("a@example.com", {"first name": "Jo"})
    assert r.subject == "Hi Jo"
    assert r.text == "Dear Jo,\n{not a field}\n"


def test_template_needs_subject_line(tmp_path):
    with pytest.raises(TemplateError, match="Subject:"):
        make_template(tmp_path, "Hello {name}\n")


def test_template_unknown_and_stray_braces(tmp_path):
    t = make_template(tmp_path, "Subject: x\nHello {name} {nmae}\n")
    with pytest.raises(TemplateError, match=r"\{nmae\}"):
        t.check_columns(["name", "email"])
    t2 = make_template(tmp_path, "Subject: x\nHello {name\n")
    with pytest.raises(TemplateError, match="lone"):
        t2.check_columns(["name"])


def test_subject_never_contains_line_breaks(tmp_path):
    t = make_template(tmp_path, "Subject: About {topic}\nbody\n")
    assert t.render("a@example.com", {"topic": "one\ntwo\r\nthree"}).subject == "About one two three"


def test_fingerprint_changes_with_content(tmp_path):
    t = make_template(tmp_path, "Subject: x {a}\nbody {a}\n")
    one = t.render("a@example.com", {"a": "1"}).fingerprint()
    assert one == t.render("A@Example.com", {"a": "1"}).fingerprint()
    assert one != t.render("a@example.com", {"a": "2"}).fingerprint()


@pytest.mark.parametrize("addr,ok", [
    ("someone@example.com", True), ("first.last+tag@sub.example.org", True),
    ("no-at.example.com", False), ("two@@example.com", False), ("space in@example.com", False),
    ("someone@example", False), ("a..b@example.com", False), ("", False), ("x@-bad.com", False),
])
def test_email_validation(addr, ok):
    assert valid_email(addr) is ok


def test_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "c.ini"
    p.write_text("[smtp]\npassword = CHANGE_ME\n", encoding="utf-8")
    s = config.load(p)
    with pytest.raises(config.ConfigError, match="MAILROWS_SMTP_PASSWORD"):
        s.secret("smtp", "password")
    monkeypatch.setenv("MAILROWS_SMTP_PASSWORD", "from-env")
    assert s.secret("smtp", "password") == "from-env"
    monkeypatch.setenv("MAILROWS_AIRTABLE_FIELDS_X", "y")
    assert config.env_name("airtable.fields", "x") == "MAILROWS_AIRTABLE_FIELDS_X"


def test_init_creates_a_working_starter(tmp_path, capsys):
    folder = tmp_path / "start"
    assert cli.main(["init", str(folder)]) == 0
    names = sorted(p.name for p in folder.iterdir())
    assert names == [".gitignore", "config.ini", "events.csv", "template.html", "template.txt"]
    assert "config.ini" in (folder / ".gitignore").read_text()
    # the starter dry-runs cleanly (one sample row has a deliberately bad address)
    code = cli.main(["send", "--dry-run", "-c", str(folder / "config.ini")])
    out = capsys.readouterr().out
    assert code == 1
    assert "4 email(s) would be sent" in out
    assert sorted(p.name for p in folder.iterdir()) == names


def test_send_without_config_explains(tmp_path, capsys):
    assert cli.main(["send", "-c", str(tmp_path / "missing.ini")]) == 2
    assert "config.example.ini" in capsys.readouterr().out


def test_smtp_check_finds_working_port(smtp):
    lines = []
    best = smtpcheck.check("127.0.0.1", "", "", timeout=3,
                           combos=[(smtp.port, "starttls"), (smtp.port, "none")],
                           out=lambda *a, end="\n": lines.append(" ".join(map(str, a))))
    text = "\n".join(lines)
    assert "doesn't offer STARTTLS" in text
    assert best is not None and best.security == "none"


def test_smtp_check_refuses_password_over_plain_connection(smtp):
    res = smtpcheck.try_one("127.0.0.1", smtp.port, "none", "me@example.com", "secret", 3)
    assert res.connected and res.logged_in is None
    assert "unencrypted" in res.message


def test_smtp_check_explains_timeouts_and_refusals():
    assert "firewall" in smtpcheck.explain(socket.timeout("timed out"), 587, "starttls")
    assert "antivirus" in smtpcheck.explain(TimeoutError(), 465, "ssl")
    assert "refused" in smtpcheck.explain(ConnectionRefusedError(), 25, "none")


def test_smtp_check_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    res = smtpcheck.try_one("127.0.0.1", port, "starttls", "", "", 3)
    assert not res.connected
    assert "refused" in res.message or "timed out" in res.message
