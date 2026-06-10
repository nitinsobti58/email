#!/usr/bin/env python3
# ^ Shebang: lets you run "./sender_frequency.py" directly via python3.
"""
sender_frequency.py
===================
For every SENDER, count how many emails they sent you across your ENTIRE
mailbox (all folders), so you can clean up the highest-frequency senders first.

What it does
------------
- Connects to each mailbox over IMAP (Gmail / iCloud / AOL), READ-ONLY.
- Walks every folder and counts messages PER SENDER ADDRESS (not just per
  domain), recording where they appear and when.
- Computes a simple "frequency" = total_messages / 100 (a scaled count you can
  sort on -- a sender with 250 emails has frequency 2.5).
- Writes one CSV per account plus a combined CSV, sorted so the senders you get
  the MOST mail from are at the top.

Whole-mailbox counting (important!)
-----------------------------------
- Gmail: every message lives in "[Gmail]/All Mail", and also appears under each
  of its labels. To avoid double-counting we scan ONLY All Mail -- that single
  folder IS the whole mailbox. (Note: Gmail's All Mail excludes Spam/Trash.)
- iCloud / AOL: a message normally lives in exactly one folder, so we iterate
  EVERY selectable folder and sum. Only non-selectable containers (\\Noselect)
  are skipped -- Sent/Drafts/Trash are included, so your OWN address can show up
  as a "sender" (just ignore your own address in the results).

Safety
------
- Read-only: folders are opened readonly=True and only headers are fetched with
  BODY.PEEK, so nothing is marked read and no bodies are downloaded.

Usage
-----
    python3 sender_frequency.py --accounts gmail icloud aol
    python3 sender_frequency.py --accounts gmail --limit 500   # quick test
    python3 sender_frequency.py --accounts icloud --since 3    # last 3 years
    python3 sender_frequency.py --accounts aol --list-folders  # which folders

Outputs
-------
    sender_freq_<account>.csv   one row per sender for that account
    sender_frequency.csv        combined across all accounts (account column)
"""

# --- Imports -----------------------------------------------------------------
import argparse
import csv
import sys
from collections import defaultdict  # dict that auto-creates missing entries

import imaplib  # for imaplib.IMAP4.error in login error handling

# Reuse the already-written, already-tested helpers instead of duplicating them.
# Importing these modules is side-effect-free (their code runs under
# `if __name__ == "__main__"`), so nothing executes just by importing.
from inbox_inventory import (
    PROVIDERS,  # host/port/default-folder per provider
    load_env,  # populate os.environ from a .env file
    get_credentials,  # env -> .env -> getpass
    connect,  # logged-in IMAP4_SSL connection
    list_folders,  # raw decoded LIST lines
    parse_message,  # raw header bytes -> dict (name/address/domain/date/...)
    imap_since,  # IMAP SINCE date string N years back
    FETCH_FIELDS,  # the BODY.PEEK header-only fetch spec
    BATCH,  # messages per server round-trip
)
from inbox_cleanup import parse_folder_line, imap_quote  # folder parsing + quoting


# --- Which folders make up "the whole mailbox" for each account --------------
def folders_to_scan(imap, account):
    """Return the list of folder names whose union is the entire mailbox.

    Gmail -> just All Mail (scanning labels too would double-count).
    iCloud / AOL -> every selectable folder (skip only \\Noselect containers).
    """
    if account == "gmail":
        return [PROVIDERS["gmail"]["folder"]]  # already quoted in PROVIDERS
    out = []
    for line in list_folders(imap):
        parsed = parse_folder_line(line)
        if parsed is None:
            print(f"    (could not parse folder line: {line!r})", file=sys.stderr)
            continue
        flags, name = parsed
        # \Noselect marks a container you cannot open (e.g. "[Gmail]") -- skip it.
        if r"\noselect" in flags:
            continue
        out.append(name)
    return out


# --- Scan one account, aggregating per sender across all its folders ----------
def scan_account(imap, account, limit=None, since_years=None):
    """Return a dict keyed by sender address -> aggregated stats for that sender."""
    folders = folders_to_scan(imap, account)

    # defaultdict auto-creates a fresh record the first time we see a sender, so
    # `agg[key]` below never raises KeyError. The factory (a lambda) returns the
    # per-sender record shape.
    agg = defaultdict(
        lambda: {
            "name": "",
            "domain": "",
            "count": 0,
            "first": None,
            "last": None,
            "mailing_list": False,
            "likely_account": False,
            "unsub_link": "",
            "unsub_link_date": None,
            "folders": set(),  # distinct folders this sender appears in
        }
    )

    for folder in folders:
        # readonly=True is the hard read-only guarantee; imap_quote handles
        # folder names that contain spaces (or a trailing space, like iCloud's).
        typ, _ = imap.select(imap_quote(folder), readonly=True)
        if typ != "OK":
            print(f"    could not open {folder!r}; skipping", file=sys.stderr)
            continue

        # Server-side date filter (faster) if --since was given, else everything.
        if since_years:
            typ, data = imap.search(None, "SINCE", imap_since(since_years))
        else:
            typ, data = imap.search(None, "ALL")
        if typ != "OK" or not data or data[0] is None:
            continue

        ids = data[0].split()
        if limit:
            ids = ids[-limit:]  # most recent N per folder (handy for testing)
        total = len(ids)
        if total == 0:
            continue

        # Strip surrounding quotes for a clean folder label in the CSV.
        folder_label = folder.strip('"')

        done = 0
        for i in range(0, total, BATCH):
            chunk = ids[i : i + BATCH]
            msg_set = b",".join(chunk).decode()
            typ, resp = imap.fetch(msg_set, f"({FETCH_FIELDS})")
            for item in resp:
                # imaplib mixes useful tuples with separator junk; keep only
                # tuples whose second element holds the raw header bytes.
                if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                    continue
                info = parse_message(item[1])
                # Key by the sender's full address; fall back to domain, then
                # a placeholder, so a missing From never crashes the run.
                key = info["address"] or info["domain"] or "(unknown)"
                rec = agg[key]
                rec["count"] += 1
                rec["folders"].add(folder_label)
                # Fill name/domain once (first non-empty value we see).
                if not rec["name"] and info["name"]:
                    rec["name"] = info["name"]
                if not rec["domain"] and info["domain"]:
                    rec["domain"] = info["domain"]
                # Track earliest/latest dates. ISO strings ("2024-01-31") sort
                # correctly with < / > because they're year-first.
                d = info["date"]
                if d:
                    if rec["first"] is None or d < rec["first"]:
                        rec["first"] = d
                    if rec["last"] is None or d > rec["last"]:
                        rec["last"] = d
                # "sticky OR": once True, stays True.
                rec["mailing_list"] = rec["mailing_list"] or info["has_unsub"]
                rec["likely_account"] = rec["likely_account"] or info["is_signup"]
                # Keep the unsubscribe link from the most recent message with one.
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
            # end="\r" overwrites the same line so progress doesn't spam output.
            print(
                f"    {account} :: {folder_label}  {done}/{total}",
                file=sys.stderr,
                end="\r",
            )
        print(file=sys.stderr)  # newline after each folder finishes.

    return agg


# --- CSV writers -------------------------------------------------------------
# Column order is shared between the per-account and combined files.
_BASE_HEADER = [
    "sender_address",
    "sender_name",
    "domain",
    "total_messages",
    "frequency",  # total_messages / 100
    "first_seen",
    "last_seen",
    "folder_count",
    "folders",
    "mailing_list",
    "likely_account",
    "unsubscribe_link",
]


def _sorted_rows(agg):
    """Sort senders by message count, highest first (high frequency first)."""
    return sorted(agg.items(), key=lambda kv: kv[1]["count"], reverse=True)


def _row_values(address, r):
    """Turn one (address, record) pair into the list of CSV cell values."""
    folders = sorted(r["folders"])
    return [
        address,
        r["name"],
        r["domain"],
        r["count"],
        round(r["count"] / 100, 2),  # the requested frequency = count / 100
        r["first"] or "",
        r["last"] or "",
        len(folders),
        "; ".join(folders),  # which folders this sender's mail lives in
        "yes" if r["mailing_list"] else "",
        "yes" if r["likely_account"] else "",
        r["unsub_link"],
    ]


def write_account_csv(path, agg):
    """Write one account's per-sender CSV, sorted high-frequency first."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_BASE_HEADER)
        for address, r in _sorted_rows(agg):
            w.writerow(_row_values(address, r))


def write_combined_csv(path, per_account):
    """Write the combined CSV across all accounts (adds a leading 'account')."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account"] + _BASE_HEADER)
        for account, agg in per_account.items():
            for address, r in _sorted_rows(agg):
                w.writerow([account] + _row_values(address, r))


# --- Main orchestration ------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Count emails per sender across the whole mailbox and rank by frequency."
    )
    ap.add_argument(
        "--accounts",
        nargs="+",
        required=True,
        choices=list(PROVIDERS),
        help="Which mailboxes to scan (gmail icloud aol).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        help="Only scan the most recent N messages per folder (good for testing).",
    )
    ap.add_argument(
        "--since",
        type=float,
        metavar="YEARS",
        help="Only count mail from the last N years (server-side, faster).",
    )
    ap.add_argument(
        "--list-folders",
        action="store_true",
        help="List the folders that WOULD be scanned for each account, then exit.",
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
            print("  -> use an APP-SPECIFIC password and enable IMAP.", file=sys.stderr)
            continue

        # --list-folders short-circuit: show scope and move on.
        if args.list_folders:
            print(f"\nFolders that would be scanned for {account}:", file=sys.stderr)
            for fol in folders_to_scan(imap, account):
                print(f"    {fol.strip(chr(34))}", file=sys.stderr)
            imap.logout()
            continue

        print(f"  scanning {account} (all folders) ...", file=sys.stderr)
        try:
            agg = scan_account(imap, account, limit=args.limit, since_years=args.since)
        except Exception as e:
            # Per-account isolation: one failure never aborts the others.
            print(f"  scan failed for {account}: {e}", file=sys.stderr)
            imap.logout()
            continue

        if agg:
            per_account[account] = agg
            out = f"{outdir}/sender_freq_{account}.csv"
            write_account_csv(out, agg)
            print(f"  wrote {out}  ({len(agg)} unique senders)", file=sys.stderr)
        else:
            print(f"  no messages found for {account}", file=sys.stderr)
        imap.logout()

    if per_account and not args.list_folders:
        combined = f"{outdir}/sender_frequency.csv"
        write_combined_csv(combined, per_account)
        print(f"\nCombined ranking written: {combined}", file=sys.stderr)


if __name__ == "__main__":
    main()
