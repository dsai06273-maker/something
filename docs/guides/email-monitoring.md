# Employer Email Monitoring

AutoApply can watch your inbox for replies from employers and update the status of matching applications — interview invitations, offers and rejections — so your dashboard reflects reality without manual bookkeeping.

## Privacy and safety

- **Read-only.** The mailbox is opened in read-only mode and messages are fetched with `BODY.PEEK`. AutoApply never sends, moves, deletes, or marks mail as read.
- **Local.** Mail is downloaded and classified on your computer by built-in rules. No email content is sent to any AI provider or any server.
- **Minimal storage.** For relevant replies only the sender, subject and a 300-character snippet are stored in `~/.autoapply/autoapply.db`. Everything else is remembered by Message-ID only, with no content.
- **Password handling.** The password is stored in your OS keyring (or, if no keyring is available, a `0600` file at `~/.autoapply/.email_secret`). It is never written to `config.json` and never returned by the API.

## Setup

1. Open **Settings → Employer Email Monitoring**.
2. Pick your provider and enter your address and an **app password** (see below).
3. Click **Test connection**, then toggle **Enable email monitoring** and **Save**.

| Provider | Notes |
|----------|-------|
| Gmail | Enable 2-Step Verification, then create an [app password](https://myaccount.google.com/apppasswords). IMAP is on by default. |
| iCloud | Generate an app-specific password at appleid.apple.com. |
| Yahoo | Generate an app password in Account Security. |
| Outlook / Office 365 | Many accounts have disabled password-based IMAP and require OAuth, which is **not supported yet**. |
| Custom | Any IMAP-over-SSL server (host + port, usually 993). |

## What it does

Each check (default every 15 minutes, or **Check now**) looks at mail from the last *N* days (default 14) that it has not seen before, then:

1. **Classifies** each message: `rejected`, `interview`, `offer`, `assessment`, `acknowledgement`, or other.
2. **Matches** it to one of your open applications using the company name (sender domain, sender name, subject, body) with the job title as a tiebreaker. A job title alone is never enough, and generic ATS domains (Greenhouse, Lever, Workday, …) are not treated as evidence.
3. **Updates the status** only when the classification is confident (default ≥ 0.8), the match is unambiguous, and the change is allowed.

Allowed changes: `applied`/`reviewed`/`manual_required` → `interview`; any in-progress status → `offer` or `rejected`. Statuses are **never downgraded** (an `offer` is not overwritten by a later rejection email), and `accepted`, `withdrawn`, `error`, `skipped` and `saved` are never touched.

Anything uncertain — ambiguous company, low confidence, or automatic updates switched off — appears under **Suggested status changes** where you can **Apply** or **Dismiss** it. Every change (manual or from email) is recorded in the application's **Status history**, and the email's subject is appended to the application's notes rather than replacing them.

## Limitations

- Classification is rule-based and English-only; unusual phrasing will be missed (it will not be guessed).
- Replies from recruiters at a different company than the one you applied to (agencies) are matched only if the application's company is named in the email.
- Password / app-password IMAP only; no OAuth.
