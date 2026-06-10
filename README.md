# inbox_inventory

A single-file, **standard-library-only** Python tool that scans your email
accounts over IMAP and builds a CSV inventory of every sender — so you can
decide what to **unsubscribe** from, which accounts to **migrate**, and which to
**close**.

No `pip install`. No dependencies. No build step. Just Python 3.9+.

---

## What it does

- Connects to each mailbox over **IMAP** (Gmail / iCloud / AOL supported out of
  the box).
- Fetches **only message headers** — `From`, `Date`, `Subject`,
  `List-Unsubscribe`, `List-Id`. It never downloads message bodies.
- Groups senders **by domain** and for each one records:
  - how many messages they sent,
  - first and last seen dates,
  - whether it's a **mailing list** (has unsubscribe headers),
  - the actual **unsubscribe link** (from the most recent message that had one),
  - whether it looks like an **account you signed up for** (subjects like
    *welcome*, *verify*, *your receipt*, *order confirmation*, …).
- Writes:
  - `senders_<account>.csv` — one file per account, and
  - `worklist.csv` — a combined file across all accounts with blank
    `action` / `new_email` / `migrated` / `notes` columns for you to fill in by
    hand to drive your cleanup.

### Safety guarantees

- **Read-only.** Folders are opened with `readonly=True` and headers are fetched
  with `BODY.PEEK`, so **nothing is marked as read and nothing is altered** in
  your mailbox.
- **Local-only.** Your data never leaves your machine. Passwords are entered at
  the prompt (hidden via `getpass`) or read from a local `.env`, and are never
  stored or printed.

---

## Requirements

- **Python 3.9+** (standard library only — verify with `python3 --version`).
- An **app-specific password** for each account (NOT your normal login
  password):

  | Provider | How to create an app password |
  |----------|-------------------------------|
  | **Gmail** | Enable 2FA, then create an *App Password (Mail)*. Also enable IMAP: Gmail settings → *Forwarding and POP/IMAP* → *Enable IMAP*. |
  | **iCloud** | [appleid.apple.com](https://appleid.apple.com) → *Sign-In & Security* → *App-Specific Passwords*. |
  | **AOL** | [login.aol.com](https://login.aol.com) → *Account Security* → *Generate app password*. |

---

## How to run

From the project directory:

```bash
# Scan all three accounts (full scan — can be large/slow for Gmail "All Mail")
python3 inbox_inventory.py --accounts gmail icloud aol

# Quick test run: only the most recent 500 messages
python3 inbox_inventory.py --accounts gmail --limit 500

# Only mail from the last 3 years (filtered server-side, so it's faster)
python3 inbox_inventory.py --accounts gmail icloud --since 3

# Discover the real folder names on an account
python3 inbox_inventory.py --accounts aol --list-folders

# Override which folder to scan (e.g. just the inbox)
python3 inbox_inventory.py --accounts icloud --folder INBOX

# Write the CSV files somewhere other than the current directory
python3 inbox_inventory.py --accounts gmail --outdir ./reports
```

> **Tip:** `--limit` is the fastest way to do an end-to-end sanity check against
> a real mailbox before committing to a full scan.

### All options

| Flag | Description |
|------|-------------|
| `--accounts gmail icloud aol` | **Required.** One or more mailboxes to scan. |
| `--folder NAME` | Override the folder to scan (applies to all accounts). |
| `--limit N` | Only scan the most recent **N** messages (good for testing). |
| `--since YEARS` | Only scan mail from the last **N** years (server-side filter, faster). |
| `--list-folders` | List the folder names for the chosen accounts and exit. |
| `--env-file PATH` | Path to a `.env` file with credentials (default: `.env`). |
| `--outdir DIR` | Directory to write CSV files into (default: current directory). |

Run `python3 inbox_inventory.py --help` to see this list any time.

---

## Credentials

When the tool needs a password it resolves it in this order, stopping at the
first source that has it:

1. A real **environment variable**,
2. a **`.env` file** in the project directory (gitignored), then
3. an interactive **prompt** (hidden input via `getpass`).

The environment variable / `.env` names are derived from the account name:

| Account | Email variable | Password variable |
|---------|----------------|-------------------|
| `gmail` | `GMAIL_EMAIL` | `GMAIL_APP_PASSWORD` |
| `icloud` | `ICLOUD_EMAIL` | `ICLOUD_APP_PASSWORD` |
| `aol` | `AOL_EMAIL` | `AOL_APP_PASSWORD` |

Example `.env` (this file is **gitignored** — never commit real credentials):

```dotenv
GMAIL_EMAIL=you@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
ICLOUD_EMAIL=you@icloud.com
ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop
```

A real environment variable always wins over a value in `.env` (the loader uses
`setdefault`, so it never clobbers what's already set in your shell).

---

## Output columns

**`senders_<account>.csv`** (sorted by message count, biggest senders first):

| Column | Meaning |
|--------|---------|
| `domain` | The sender's domain (e.g. `marketing.example.com`). |
| `sender_name` | A sample display name seen for that domain. |
| `sample_address` | A sample full email address. |
| `messages` | How many messages came from that domain. |
| `first_seen` / `last_seen` | Earliest / latest message date (`YYYY-MM-DD`). |
| `mailing_list` | `yes` if it had unsubscribe / list headers. |
| `likely_account` | `yes` if a subject matched a signup keyword. |
| `unsubscribe_link` | The unsubscribe URL from the most recent message that had one. |

**`worklist.csv`** has all of the above (plus an `account` column) and adds four
**blank** columns for you to fill in: `action`, `new_email`, `migrated`,
`notes`.

---

## How it works (the pipeline)

```
main
 └─ for each account:
      connect ──► scan ──► aggregate by domain ──► write_account_csv
 └─ write_worklist   (one combined file across all accounts)
```

- Provider connection details live in the table-driven `PROVIDERS` dict — add a
  new provider by adding one entry.
- Messages are fetched in batches of 500 per server round-trip.
- Aggregation is keyed by sender **domain** using a `defaultdict`.

The source file (`inbox_inventory.py`) is **heavily commented to teach the
Python concepts** it uses (list comprehensions, `defaultdict`, context managers,
f-strings, slicing, the `if __name__ == "__main__"` guard, and more) — read it
top to bottom as a guided tour.

---

## Notes & limitations

- There is **no test suite** and **no linter** configured. `--limit` is the
  practical way to do a fast end-to-end check.
- Gmail's default folder is `[Gmail]/All Mail` (everything you've ever received
  — the most complete view, but large and slow). Use `--folder INBOX` or
  `--since` to narrow it down.
- The "likely account" flag is a heuristic based on subject keywords — treat it
  as a hint, not a guarantee.
```
