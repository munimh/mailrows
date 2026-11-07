# mailrows

[![PyPI version](https://img.shields.io/pypi/v/mailrows)](https://pypi.org/project/mailrows/)
[![Python versions](https://img.shields.io/pypi/pyversions/mailrows)](https://pypi.org/project/mailrows/)
[![License: MIT](https://img.shields.io/pypi/l/mailrows)](https://github.com/munimh/mailrows/blob/main/LICENSE)
[![tests](https://github.com/munimh/mailrows/actions/workflows/tests.yml/badge.svg)](https://github.com/munimh/mailrows/actions/workflows/tests.yml)

Mail merge from Excel or CSV in Python: send one templated email per row of a spreadsheet
through your own SMTP server, safely, from your own computer.

![Terminal recording: mailrows init creates a starter folder, then mailrows send --dry-run shows the table of who will be emailed, the row skipped for a bad address, and one fully rendered sample email](https://raw.githubusercontent.com/munimh/mailrows/main/docs/demo.gif)

- **Preview first.** Every row is checked, a table shows who will get an email, one email is
  shown in full, and nothing goes until you type `yes`.
- **Never twice.** A local send log records every email before and after it goes. Reruns skip
  anything already sent, even if you cleared the status column, edited the sheet or the
  process was killed halfway.
- **Bad rows are skipped, not sent.** Invalid addresses, empty required fields and duplicate
  IDs are listed with the reason.
- **Test and dry-run modes** that change nothing.
- **Optional follow-up step** after each email: update an Airtable record, call a webhook, or
  append to a CSV. If the email went but the update failed, only the update is retried.
- Plain Python 3.10+, one dependency (`openpyxl`). Windows, macOS and Linux.

## Quick start (5 minutes)

**1. Install** (needs Python 3.10 or newer):

```bash
pipx install mailrows        # or: python -m pip install mailrows
```

**2. Make a starter folder** with a config file, a sample sheet and a template:

```bash
mailrows init my-mailing
cd my-mailing
```

**3. Fill in your mail server** in `config.ini`, under `[smtp]` and `[sender]`. Not sure which
port or security setting to use? Ask the tool; it tries the usual ones and sends nothing:

```bash
mailrows smtp-check
```

**4. See what would happen.** Nothing is sent and no file changes:

```bash
mailrows send --dry-run
```

The sample sheet has one row with a broken address on purpose, so you can see how bad rows
are reported.

**5. Send every email to yourself first**, marked `[TEST]`. No files or logs change:

```bash
mailrows send --test-to your.name@example.com
```

**6. Send for real.** Put your own rows in `events.csv` (or point `[input] file` at an `.xlsx`;
[`examples/events.xlsx`](https://github.com/munimh/mailrows/blob/main/examples/events.xlsx) is the same sample as a spreadsheet), then:

```bash
mailrows send
```

`python -m mailrows ...` works everywhere `mailrows ...` does.

### On Windows without a terminal

The `windows` folder has double-click helpers. Run `install.bat` once, copy the four numbered
`.bat` files and `_python.cmd` into the folder that holds your `config.ini`, then use
`1 check mail settings`, `2 dry run`, `3 send a test to me` and `4 send for real`.

## The spreadsheet

Any `.xlsx` or `.csv` file with a heading row. The config says which column is which:

```ini
[input]
file = events.csv
id_column = id               ; unique per row, never reused
email_column = email
status_column = status       ; mailrows writes progress here (optional)
required_columns = first_name, email
```

Every column can be used in the template. The ID is what makes reruns safe, so give each row
an ID that stays with it (a booking number, a member number). Don't renumber rows.

## The template

A text file. The first line is the subject, the rest is the body. `{column name}` is replaced
with that row's value, matching the heading exactly. `{{` and `}}` give a literal brace.

```text
Subject: Your place at the Riverside Maker Day, {first_name}
Hi {first_name},

You're booked into: {session}.
```

If the template uses a placeholder that isn't a column, the run stops before anything is sent
and tells you which one. For an HTML version too, set `[template] html = template.html`; it
uses the same placeholders, and values are HTML-escaped. The plain text is always included.

## What a run does

1. Reads the sheet and the template; stops on any unknown placeholder or missing column.
2. Checks every row: an ID, no duplicate IDs, a valid address, required fields filled, not
   already sent. Problems are listed and those rows are skipped.
3. If a follow-up step is enabled, checks it can be done for every row (for Airtable, that the
   matching record exists). Rows that fail the check are skipped.
4. Shows the preview table and one fully rendered email, then asks you to type `yes`.
5. Sends one by one, `delay_seconds` apart, writing the send log and the status column after
   each email.

### Modes

| Command | Sends | Changes files or logs | Follow-up step |
|---|---|---|---|
| `mailrows send` | to each row | yes | runs |
| `mailrows send --dry-run` | nothing | no | only its read-only check |
| `mailrows send --test-to ADDRESS` | all to ADDRESS, subject starts `[TEST]`, no BCC | no | skipped |

### How "never twice" works

The send log (`mailrows-log.sqlite3` next to your config) is the final word, not the
spreadsheet. For each row it keeps the row ID and a hash of the exact message (recipient,
subject and body).

- Before an email is handed to the server, a "sending" record is saved. After the server
  accepts it, the record becomes "sent". Then the status column is updated.
- A rerun skips every row ID the log knows, whatever the status column says.
- It also skips a row whose message is identical to one already sent under another ID (for
  example, a row copied with a new ID).
- If the server refuses an email, the record is removed and the next run tries again. The same
  happens if you press Ctrl+C before the email has started going.
- If the mail server can't be reached twice in a row, the run stops instead of waiting on every
  remaining row. Those rows go on the next run.
- If the process is killed, or the connection drops while the server is receiving the email,
  the record stays "sending". mailrows can't know whether it arrived, so it doesn't guess: the
  row is marked `check: may have been sent` and skipped. Look in your Sent folder, then:

  ```bash
  mailrows resolve E-1001 --sent       # it arrived: never send it
  mailrows resolve E-1001 --not-sent   # it didn't: send it on the next run
  ```

If the spreadsheet is open in Excel when a status needs writing, mailrows carries on (the log
already protects you) and the next run fixes the status column. For a very large `.xlsx`, set
`[input] status_save_every = 20` to save the status column every 20 emails instead of after
each one; the send log is still updated after every email. Close the file before a run to
avoid this; a real run refuses to start if it can't write to the sheet.

To send to a row again on purpose, give it a new ID.

## Settings

All settings live in `config.ini` (copy it from `config.example.ini`). The main ones:

```ini
[smtp]
host = smtp.example.com
port = 587
security = starttls          ; ssl, starttls or none
username = your.name@example.com
password = CHANGE_ME         ; or the MAILROWS_SMTP_PASSWORD environment variable

[sender]
from_name = Example Community Events
from_email = events@example.com
reply_to = hello@example.com
bcc =                        ; optional; real sends only, never shown to recipients
delay_seconds = 2
```

Any setting can come from an environment variable named `MAILROWS_<SECTION>_<KEY>`, which wins
over the file: `MAILROWS_SMTP_PASSWORD`, `MAILROWS_AIRTABLE_TOKEN`, and so on.

## Follow-up steps (hooks)

Off by default. Turn them on with a comma-separated list:

```ini
[hooks]
enabled = airtable
```

### Airtable

Updates one record per sent email.

```ini
[airtable]
base_id = appXXXXXXXXXXXXXX
table = Attendees            ; name or ID
match_field = Ticket ID      ; the Airtable field to find the record by
match_column = id            ; the spreadsheet column with the same value
token = CHANGE_ME            ; or MAILROWS_AIRTABLE_TOKEN

[airtable.fields]
Invite status = Invited      ; a fixed value
Session = {session}          ; from the row
Invite sent on = {_sent_date}
```

- Every record is looked up **before any email is sent**. A row with no matching record, or
  more than one, is skipped and listed. A bad token or table name stops the run.
- Calls stay under 5 per second and are retried with backoff when Airtable is busy.
- If an email went but its update failed, the row is marked `sent, update pending`. The next
  run retries only the update. The email is never sent again.

Built-in values for any hook: `{_sent_at}`, `{_sent_date}`, `{_row_id}`, `{_recipient}`,
`{_subject}`.

Create the token at airtable.com/create/tokens with the `data.records:read` and
`data.records:write` scopes, limited to the one base.

### Webhook

Posts `{"row_id", "recipient", "subject", "sent_at", "row": {...}}` as JSON after each email.

```ini
[webhook]
url = https://hooks.example.com/mailrows
header_name = X-Mailrows-Token   ; optional shared secret
header_value = CHANGE_ME         ; or MAILROWS_WEBHOOK_HEADER_VALUE
columns = id, email              ; default: all columns
```

A retry can deliver the same row twice, so treat `row_id` as an idempotency key.

### CSV append

```ini
[csv_append]
path = sent-copy.csv
columns = id, email, _sent_at
```

### Writing your own

Subclass `mailrows.hooks.Hook`: `prepare(rows)` runs before any email and returns the rows it
can't handle; `run(row)` runs after each email and raises on failure. Register it in
`load_hooks`. The pending/retry bookkeeping is handled for you.

## smtp-check

```bash
mailrows smtp-check                       # uses [smtp] from config.ini
mailrows smtp-check --host smtp.example.com --username your.name@example.com
```

Tries port 465 with SSL, 587 with STARTTLS, 25 and 2525, connects, tests the login without
sending, and tells you which `port` and `security` to use. It never sends a password over an
unencrypted connection. If every port times out, something on your computer or network (a
firewall, an antivirus "mail shield", an office or hotel network) is blocking outgoing mail.

## Why not Zapier or Make?

They are great for many things, but for "email everyone on this list":

- **Cost.** Each row is a billed task. A few hundred emails a month can need a paid plan.
- **No real preview.** You find out what the 200th email looks like after it's sent.
- **Reruns are risky.** Replaying a failed run can email people twice; a half-finished run is
  hard to pick up safely.
- **Your data stays with you.** The spreadsheet, the addresses and the send log stay on your
  computer, and mail goes straight from your own mail server.

mailrows is not a newsletter platform: there is no unsubscribe handling, open tracking or
bounce processing. For marketing email to large lists, use a service built for that.

## Exit codes

`0` everything went, `1` some rows had problems (listed), `2` stopped before sending (setup
problem), `3` cancelled at the prompt.

## Development

```bash
python -m pip install -e ".[test]"
python -m pytest
```

The tests run against a fake SMTP server and a fake Airtable API on `127.0.0.1`; nothing
leaves your machine.

## Licence

MIT. See [LICENSE](https://github.com/munimh/mailrows/blob/main/LICENSE).
