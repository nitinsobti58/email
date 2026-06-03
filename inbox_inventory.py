#!/usr/bin/env python3
"""
inbox_inventory.py
==================
Build a CSV inventory of every sender across your email accounts so you can
decide what to unsubscribe from, which accounts to migrate, and which to close.

What it does
------------
- Connects to each mailbox over IMAP (Gmail / iCloud / AOL supported out of box).
- Fetches ONLY message headers (From, Date, Subject, List-Unsubscribe, List-Id).
  It never downloads message bodies and never marks anything as read.
- Groups senders by domain, counts them, records first/last seen, flags mailing
  lists, captures the actual unsubscribe link, and flags likely account signups
  (welcome / verify / receipt-style subjects).
- Writes one CSV per account plus a combined `worklist.csv` with blank
  action / new_email / migrated / notes columns for you to drive the cleanup.

Safety
------
- Read-only: the folder is opened with readonly=True and fetches use BODY.PEEK,
  so nothing in your mailbox is altered.
- Local-only: data never leaves your machine. Passwords are entered at the
  prompt (getpass) and are never stored or printed.

Requirements
------------
- Python 3.9+ (standard library only -- no pip installs).
- An APP-SPECIFIC password for each account (NOT your normal login password):
    Gmail : enable 2FA, then create an App Password (Mail). IMAP must be on
            (Gmail settings -> Forwarding and POP/IMAP -> Enable IMAP).
    iCloud: appleid.apple.com -> Sign-In & Security -> App-Specific Passwords.
    AOL   : login.aol.com -> Account Security -> Generate app password.

Usage
-----
    python3 inbox_inventory.py --accounts gmail icloud aol
    python3 inbox_inventory.py --accounts gmail --limit 500       # quick test run
    python3 inbox_inventory.py --accounts gmail icloud --since 3  # last 3 years only
    python3 inbox_inventory.py --accounts aol --list-folders      # see folder names
    python3 inbox_inventory.py --accounts icloud --folder INBOX   # override folder
"""

import argparse
import csv
import email
import getpass
import imaplib
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

# host/port/default-folder per provider. Gmail's "All Mail" is everything you've
# ever received (most complete, but large/slow); override with --folder INBOX
# or narrow with --since for a faster pass.
PROVIDERS = {
    "gmail": {"host": "imap.gmail.com", "port": 993, "folder": '"[Gmail]/All Mail"'},
    "icloud": {"host": "imap.mail.me.com", "port": 993, "folder": "INBOX"},
    "aol": {"host": "imap.aol.com", "port": 993, "folder": "INBOX"},
}

# Subject substrings that strongly suggest "you have an account here".
SIGNUP_KEYWORDS = (
    "welcome",
    "verify",
    "confirm",
    "activate",
    "your account",
    "account created",
    "your receipt",
    "order confirmation",
    "sign up",
    "signup",
    "get started",
    "reset your password",
    "password reset",
)

FETCH_FIELDS = "BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT LIST-UNSUBSCRIBE LIST-ID)]"
BATCH = 500  # messages fetched per server round-trip

# English month abbreviations -- IMAP SINCE wants these regardless of locale.
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
_ANGLE = re.compile(r"<([^>]+)>")


def decode_str(raw):
    """Decode a possibly MIME-encoded header (=?UTF-8?...?=) to plain text."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def extract_unsub(header_value):
    """Pull the best unsubscribe target out of a List-Unsubscribe header.

    Header looks like: <https://x.com/unsub?id=1>, <mailto:unsub@x.com?subject=no>
    Prefer an http(s) link (clickable); fall back to mailto; else raw.
    """
    if not header_value:
        return ""
    links = _ANGLE.findall(header_value) or [header_value.strip()]
    http = [l for l in links if l.lower().startswith("http")]
    if http:
        return http[0]
    mailto = [l for l in links if l.lower().startswith("mailto:")]
    if mailto:
        return mailto[0]
    return links[0] if links else ""


def imap_since(years):
    """Return an IMAP SINCE date string (e.g. '02-Jun-2023') N years back."""
    cutoff = datetime.now() - timedelta(days=round(365.25 * years))
    return f"{cutoff.day:02d}-{_MONTHS[cutoff.month - 1]}-{cutoff.year}"


def load_env(path=".env"):
    """Minimal .env loader (no dependencies). Lines look like KEY=value;
    blank lines and #comments are ignored, surrounding quotes are stripped.
    Real environment variables already set take precedence (never overridden).
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            os.environ.setdefault(key, val)  # don't clobber real env vars


def get_credentials(account):
    """Resolve credentials from env/.env, prompting for anything missing."""
    prefix = account.upper()
    user = os.environ.get(f"{prefix}_EMAIL", "").strip()
    password = os.environ.get(f"{prefix}_APP_PASSWORD", "")
    if not user:
        user = input(f"[{account}] email address: ").strip()
    if not password:
        password = getpass.getpass(f"[{account}] APP-SPECIFIC password: ")
    return user, password


def connect(provider, user, password):
    cfg = PROVIDERS[provider]
    imap = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    imap.login(user, password)
    return imap


def list_folders(imap):
    typ, data = imap.list()
    out = []
    for line in data or []:
        if isinstance(line, bytes):
            out.append(line.decode(errors="replace"))
    return out


def parse_message(raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    name, addr = parseaddr(msg.get("From", ""))
    addr = addr.lower().strip()
    domain = addr.split("@")[-1] if "@" in addr else (addr or "(unknown)")
    subject = decode_str(msg.get("Subject", "")).lower()
    when = None
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt:
            when = dt.date().isoformat()
    except Exception:
        when = None
    unsub_raw = msg.get("List-Unsubscribe")
    return {
        "name": decode_str(name),
        "address": addr,
        "domain": domain,
        "date": when,
        "has_unsub": unsub_raw is not None or msg.get("List-Id") is not None,
        "unsub_link": extract_unsub(unsub_raw),
        "is_signup": any(k in subject for k in SIGNUP_KEYWORDS),
    }


def scan(imap, folder, limit=None, since_years=None):
    typ, _ = imap.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(
            f"Could not open folder {folder!r}. "
            f"Run with --list-folders to see valid folder names."
        )

    if since_years:
        date_str = imap_since(since_years)
        print(f"    (only messages since {date_str})", file=sys.stderr)
        typ, data = imap.search(None, "SINCE", date_str)
    else:
        typ, data = imap.search(None, "ALL")

    ids = data[0].split()
    if limit:
        ids = ids[-limit:]  # most recent N (highest sequence numbers)
    total = len(ids)
    if total == 0:
        return {}

    agg = defaultdict(
        lambda: {
            "name": "",
            "address": "",
            "count": 0,
            "first": None,
            "last": None,
            "mailing_list": False,
            "likely_account": False,
            "unsub_link": "",
            "unsub_link_date": None,
        }
    )

    done = 0
    for i in range(0, total, BATCH):
        chunk = ids[i : i + BATCH]
        msg_set = b",".join(chunk).decode()
        typ, resp = imap.fetch(msg_set, f"({FETCH_FIELDS})")
        for item in resp:
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            info = parse_message(item[1])
            rec = agg[info["domain"]]
            rec["count"] += 1
            if not rec["name"] and info["name"]:
                rec["name"] = info["name"]
            if not rec["address"] and info["address"]:
                rec["address"] = info["address"]
            d = info["date"]
            if d:
                if rec["first"] is None or d < rec["first"]:
                    rec["first"] = d
                if rec["last"] is None or d > rec["last"]:
                    rec["last"] = d
            rec["mailing_list"] = rec["mailing_list"] or info["has_unsub"]
            rec["likely_account"] = rec["likely_account"] or info["is_signup"]
            # keep the unsubscribe link from the most recent message that had one
            link = info["unsub_link"]
            if link:
                if not rec["unsub_link"]:
                    rec["unsub_link"] = link
                    rec["unsub_link_date"] = d
                elif d and (
                    rec["unsub_link_date"] is None or d > rec["unsub_link_date"]
                ):
                    rec["unsub_link"] = link
                    rec["unsub_link_date"] = d
        done += len(chunk)
        print(f"    {done}/{total} messages scanned", file=sys.stderr, end="\r")
    print(file=sys.stderr)
    return agg


def write_account_csv(path, agg):
    rows = sorted(agg.items(), key=lambda kv: kv[1]["count"], reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "domain",
                "sender_name",
                "sample_address",
                "messages",
                "first_seen",
                "last_seen",
                "mailing_list",
                "likely_account",
                "unsubscribe_link",
            ]
        )
        for domain, r in rows:
            w.writerow(
                [
                    domain,
                    r["name"],
                    r["address"],
                    r["count"],
                    r["first"] or "",
                    r["last"] or "",
                    "yes" if r["mailing_list"] else "",
                    "yes" if r["likely_account"] else "",
                    r["unsub_link"],
                ]
            )


def write_worklist(path, per_account):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "account",
                "domain",
                "sender_name",
                "sample_address",
                "messages",
                "first_seen",
                "last_seen",
                "mailing_list",
                "likely_account",
                "unsubscribe_link",
                "action",
                "new_email",
                "migrated",
                "notes",
            ]
        )
        for account, agg in per_account.items():
            rows = sorted(agg.items(), key=lambda kv: kv[1]["count"], reverse=True)
            for domain, r in rows:
                w.writerow(
                    [
                        account,
                        domain,
                        r["name"],
                        r["address"],
                        r["count"],
                        r["first"] or "",
                        r["last"] or "",
                        "yes" if r["mailing_list"] else "",
                        "yes" if r["likely_account"] else "",
                        r["unsub_link"],
                        "",
                        "",
                        "",
                        "",
                    ]
                )  # action / new_email / migrated / notes


def main():
    ap = argparse.ArgumentParser(
        description="Inventory senders across mailboxes and export to CSV."
    )
    ap.add_argument(
        "--accounts",
        nargs="+",
        required=True,
        choices=list(PROVIDERS),
        help="Which mailboxes to scan (gmail icloud aol).",
    )
    ap.add_argument(
        "--folder", help="Override the folder to scan (applies to all accounts)."
    )
    ap.add_argument(
        "--limit",
        type=int,
        help="Only scan the most recent N messages (good for testing).",
    )
    ap.add_argument(
        "--since",
        type=float,
        metavar="YEARS",
        help="Only scan mail from the last N years (server-side, faster).",
    )
    ap.add_argument(
        "--list-folders",
        action="store_true",
        help="List folders for the chosen accounts and exit.",
    )
    ap.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file with credentials (default: .env).",
    )
    ap.add_argument(
        "--outdir",
        default=".",
        help="Directory to write CSV files into (default: current).",
    )
    args = ap.parse_args()

    load_env(args.env_file)
    outdir = args.outdir.rstrip("/")
    per_account = {}

    for account in args.accounts:
        user, password = get_credentials(account)
        try:
            imap = connect(account, user, password)
        except imaplib.IMAP4.error as e:
            print(f"  login failed for {account}: {e}", file=sys.stderr)
            print(
                "  -> use an APP-SPECIFIC password and make sure IMAP is enabled.",
                file=sys.stderr,
            )
            continue

        if args.list_folders:
            print(f"\nFolders for {account}:")
            for fol in list_folders(imap):
                print("  ", fol)
            imap.logout()
            continue

        folder = args.folder or PROVIDERS[account]["folder"]
        print(f"  scanning {account} :: {folder} ...", file=sys.stderr)
        try:
            agg = scan(imap, folder, limit=args.limit, since_years=args.since)
        except Exception as e:
            print(f"  scan failed for {account}: {e}", file=sys.stderr)
            imap.logout()
            continue

        if agg:
            per_account[account] = agg
            out = f"{outdir}/senders_{account}.csv"
            write_account_csv(out, agg)
            print(f"  wrote {out}  ({len(agg)} unique domains)", file=sys.stderr)
        else:
            print(f"  no messages found for {account}", file=sys.stderr)
        imap.logout()

    if per_account and not args.list_folders:
        wl = f"{outdir}/worklist.csv"
        write_worklist(wl, per_account)
        print(f"\nCombined worklist written: {wl}", file=sys.stderr)


if __name__ == "__main__":
    main()
