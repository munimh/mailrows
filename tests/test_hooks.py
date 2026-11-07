import json

import pytest

from conftest import ROWS

from mailrows import run
from mailrows.hooks.http import RateLimiter


def airtable_config(project, airtable, **fields):
    project.write_config(
        hooks="airtable",
        airtable={"base_id": "appTEST0000000000", "table": "People", "match_field": "Member ID",
                  "match_column": "id", "token": airtable.token, "api_url": airtable.url,
                  "retries": "3", "backoff_seconds": "0.01"},
        airtable__fields=fields or {"Last emailed": "{_sent_date}", "Topic": "{topic}", "Stage": "Emailed"},
    )


def seed(airtable):
    airtable.add("rec001", **{"Member ID": "A1"})
    airtable.add("rec002", **{"Member ID": "A2"})
    airtable.add("rec003", **{"Member ID": "A3"})


def test_airtable_records_updated_after_each_email(project, smtp, airtable):
    seed(airtable)
    airtable_config(project, airtable)
    assert project.run() == run.EXIT_OK
    assert len(smtp.messages) == 3
    assert [rid for rid, _ in airtable.patches] == ["rec001", "rec002", "rec003"]
    fields = airtable.records["rec002"]
    assert fields["Topic"] == "Cycling" and fields["Stage"] == "Emailed"
    assert len(fields["Last emailed"]) == 10  # YYYY-MM-DD
    assert project.status() == {"A1": "sent", "A2": "sent", "A3": "sent"}


def test_missing_airtable_record_is_found_before_any_email(project, smtp, airtable):
    airtable.add("rec001", **{"Member ID": "A1"})
    airtable.add("rec003", **{"Member ID": "A3"})
    airtable_config(project, airtable)
    assert project.run() == run.EXIT_PROBLEMS
    assert [to for _, (to,), _ in smtp.messages] == ["alex@example.com", "casey@example.com"]
    assert "no Airtable record has Member ID = A2" in project.output
    assert project.status()["A2"] == ""


def test_two_matching_records_skip_the_row(project, smtp, airtable):
    seed(airtable)
    airtable.add("rec099", **{"Member ID": "A3"})
    airtable_config(project, airtable)
    project.run()
    assert "casey@example.com" not in [m["To"] for m in smtp.parsed()]
    assert "2 Airtable records have Member ID = A3" in project.output


def test_bad_token_stops_before_any_email(project, smtp, airtable):
    seed(airtable)
    airtable_config(project, airtable)
    airtable.token = "a-different-token"
    with pytest.raises(run.SetupError, match="401"):
        project.run()
    assert smtp.messages == []


def test_partial_airtable_failure_retries_only_the_update(project, smtp, airtable):
    seed(airtable)
    airtable.fail_patch = {"rec002"}
    airtable_config(project, airtable)
    assert project.run() == run.EXIT_PROBLEMS
    assert len(smtp.messages) == 3
    assert project.status() == {"A1": "sent", "A2": "sent, update pending", "A3": "sent"}
    assert "Stage" not in airtable.records["rec002"]

    # still failing: the email is not sent again, the row stays pending
    project.run()
    assert len(smtp.messages) == 3
    assert "retry airtable update only" in project.output
    assert project.status()["A2"] == "sent, update pending"

    airtable.fail_patch = set()
    patches_before = len(airtable.patches)
    assert project.run() == run.EXIT_OK
    assert len(smtp.messages) == 3
    assert [rid for rid, _ in airtable.patches[patches_before:]] == ["rec002"]
    assert airtable.records["rec002"]["Stage"] == "Emailed"
    assert project.status()["A2"] == "sent"

    project.run()
    assert len(airtable.patches) == patches_before + 1
    assert "Nothing to send" in project.output


def test_pending_update_survives_cleared_status_column(project, smtp, airtable):
    seed(airtable)
    airtable.fail_patch = {"rec001"}
    airtable_config(project, airtable)
    project.run()
    project.write_rows(ROWS)  # someone clears the status column
    airtable.fail_patch = set()
    project.run()
    assert len(smtp.messages) == 3
    assert airtable.records["rec001"]["Stage"] == "Emailed"


def test_airtable_busy_is_retried(project, smtp, airtable):
    seed(airtable)
    airtable.busy_once = 2
    airtable_config(project, airtable)
    assert project.run() == run.EXIT_OK
    assert len(airtable.patches) == 3


def test_dry_run_and_test_mode_never_write_airtable(project, smtp, airtable):
    seed(airtable)
    airtable_config(project, airtable)
    before = project.snapshot()
    project.run(mode=run.DRY)
    project.run(mode=run.TEST, test_to="me@example.com")
    assert airtable.patches == []
    assert all(method == "GET" for _, method, _ in airtable.calls)
    assert project.snapshot() == before


def test_unknown_column_in_airtable_value_stops(project, smtp, airtable):
    seed(airtable)
    airtable_config(project, airtable, Note="{nickname}")
    with pytest.raises(run.SetupError, match="nickname"):
        project.run()
    assert smtp.messages == []


def test_airtable_calls_stay_under_five_per_second(project, smtp, airtable):
    rows = [[f"R{i}", f"Person {i}", f"p{i}@example.com", "Chess", ""] for i in range(12)]
    project.write_rows(rows)
    for i in range(12):
        airtable.add(f"rec{i:03}", **{"Member ID": f"R{i}"})
    airtable_config(project, airtable)
    project.run()
    times = [t for t, _, _ in airtable.calls]
    assert len(times) >= 13
    for i in range(len(times) - 5):
        assert times[i + 5] - times[i] >= 1.0, "more than 5 Airtable calls inside one second"


def test_rate_limiter_spacing():
    clock = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s

    lim = RateLimiter(4.5, clock=lambda: clock[0], sleep=sleep)
    for _ in range(10):
        lim.wait()
    assert clock[0] >= 9 / 4.5 - 1e-9


def test_webhook_hook_posts_each_row(project, smtp, airtable):
    project.write_config(hooks="webhook", webhook={"url": airtable.url + "/hook", "columns": "id, topic"})
    project.run()
    bodies = [b for kind, b in airtable.patches if kind == "webhook"]
    assert [b["row_id"] for b in bodies] == ["A1", "A2", "A3"]
    assert bodies[0]["row"] == {"id": "A1", "topic": "Gardening"}
    assert bodies[0]["recipient"] == "alex@example.com"
    json.dumps(bodies)


def test_csv_append_hook(project, smtp):
    project.write_config(hooks="csv_append", csv_append={"path": "copy.csv", "columns": "id, email, _sent_date"})
    project.run()
    lines = (project.folder / "copy.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,email,_sent_date"
    assert len(lines) == 4 and lines[1].startswith("A1,alex@example.com,")


def test_unknown_hook_name_stops(project, smtp):
    project.write_config(hooks="carrier_pigeon")
    with pytest.raises(Exception, match="carrier_pigeon"):
        project.run()
    assert smtp.messages == []
