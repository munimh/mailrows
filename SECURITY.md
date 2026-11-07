# Security

## Keep credentials out of the repository

mailrows needs an SMTP password and, if you use Airtable, an API token. They belong in
`config.ini` or in environment variables, never in a file you commit.

- `config.ini` is in `.gitignore`. Only `config.example.ini`, which holds placeholders like
  `CHANGE_ME`, is meant to be committed. mailrows refuses to run with a placeholder password.
- For scheduled or shared setups, prefer environment variables: `MAILROWS_SMTP_PASSWORD`,
  `MAILROWS_AIRTABLE_TOKEN`, `MAILROWS_WEBHOOK_HEADER_VALUE`. They override the file.
- Use an app password for your mailbox if your provider offers one, and an Airtable token
  limited to `data.records:read` and `data.records:write` on the one base you need.
- The send log (`*.sqlite3`) and your spreadsheet contain recipients' addresses. They are
  personal data: keep them out of git too (the default `.gitignore` does this).
- If a secret was ever committed, treat it as leaked: revoke it and create a new one. Removing
  it from history is not enough.

Before publishing a fork or a copy, a secret scanner such as
[gitleaks](https://github.com/gitleaks/gitleaks) is a quick check:

```bash
gitleaks detect --source . --redact
```

## What mailrows does to protect you

- It never sends a password over an unencrypted connection to a remote server
  (`security = none` is only allowed with no login, or for `localhost`).
- TLS certificates are verified with the system's trusted roots.
- HTTP redirects are refused for Airtable and webhook calls, so a token or secret header is
  never resent to another host. A webhook with a secret header must use `https://`.
- A password that merely contains characters like `<` is accepted; only empty values and
  obvious placeholders (`CHANGE_ME`, `<...>`) count as "not set".
- Values written to spreadsheets are stored as plain text, never as formulas.
- It talks only to the servers in your config. There is no telemetry.

## Reporting a vulnerability

Please report security problems privately, using GitHub's "Report a vulnerability" button on
the repository's Security tab, rather than in a public issue. Include steps to reproduce if you
can. You should get a reply within a week.
