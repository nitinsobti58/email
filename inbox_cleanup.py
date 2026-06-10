#!/usr/bin/env python3
# ^ Shebang: lets you run "./inbox_cleanup.py" directly; the OS uses python3.
"""
inbox_cleanup.py
================
PERMANENTLY delete old mail across your email accounts (Gmail / iCloud / AOL).

By default it deletes every message dated BEFORE January 1st of the current year,
EXCEPT messages from a sender you want to keep (default: 21131mmw@gmail.com).

  >>> THIS IS A DESTRUCTIVE TOOL. It is the deliberate opposite of <<<
  >>> inbox_inventory.py, which is read-only. Deletions here are       <<<
  >>> PERMANENT -- expunged mail does NOT go to Trash and CANNOT be    <<<
  >>> recovered. Always do a dry run first.                            <<<

How it stays safe
-----------------
- DRY RUN BY DEFAULT. Without --confirm the script only COUNTS candidates and
  writes an audit CSV; it opens folders read-only, so it is physically
  incapable of changing your mailbox.
- --confirm additionally requires you to TYPE the word DELETE at a prompt before
  anything is expunged. Only then are folders opened read-write.

Provider-aware deletion (important!)
------------------------------------
Gmail's IMAP "folders" are really labels. Marking a message \\Deleted inside a
label folder only REMOVES THE LABEL -- the message survives in "[Gmail]/All Mail".
So for Gmail we operate ONCE on All Mail, where an expunge is a true permanent
delete. For iCloud / AOL (standard IMAP) we iterate every normal folder and
expunge per folder. This is the single most important correctness detail here.

Usage
-----
    # 1) See which folders are in scope vs skipped (no deletion):
    python3 inbox_cleanup.py --accounts icloud aol --list-folders

    # 2) DRY RUN -- count what WOULD be deleted, change nothing:
    python3 inbox_cleanup.py --accounts gmail icloud aol

    # 3) DRY RUN with a per-message log so you can eyeball the candidates:
    python3 inbox_cleanup.py --accounts gmail --detail-log

    # 4) ACTUALLY DELETE (asks you to type DELETE):
    python3 inbox_cleanup.py --accounts gmail --confirm

Outputs
-------
    cleanup_audit.csv   account / folder / candidate_count / deleted_count / ...
    cleanup_detail.csv  (only with --detail-log) one row per candidate message

Requirements
------------
- Python 3.9+, standard library only (no pip installs).
- An APP-SPECIFIC password per account (same as inbox_inventory.py).
"""

# --- Imports -----------------------------------------------------------------
import argparse  # parse command-line flags
import csv  # write the audit / detail CSV files
import re  # parse the LIST folder lines
import sys  # write progress to stderr
from datetime import datetime  # compute the "Jan 1 this year" cutoff

# email.utils gives us small parsers for the From/Date headers (detail log only).
from email.utils import parseaddr, parsedate_to_datetime

import imaplib  # we need imaplib.IMAP4.error for login error handling

# Reuse the helpers already written (and tested) in the inventory tool instead of
# duplicating them. Importing the module is side-effect-free because everything
# there runs under `if __name__ == "__main__"`, so nothing executes on import.
from inbox_inventory import (
    PROVIDERS,  # host/port/default-folder per provider
    load_env,  # populate os.environ from a .env file
    get_credentials,  # env -> .env -> getpass prompt
    connect,  # returns a logged-in IMAP4_SSL connection
    list_folders,  # returns raw decoded LIST lines
    _MONTHS,  # English month abbreviations for IMAP date strings
)
from inbox_inventory import decode_str  # decode MIME-encoded Subject for the log


# --- Configuration constants -------------------------------------------------
BATCH = 500  # UIDs per server round-trip (mirrors inbox_inventory)
DEFAULT_KEEP_FROM = "21131mmw@gmail.com"  # sender whose mail we never delete

# Per-provider deletion STRATEGY. "all_mail" = Gmail (delete once via All Mail);
# "iterate" = standard IMAP (loop every normal folder). This is what makes the
# tool provider-aware -- see the module docstring for why Gmail is special.
STRATEGY = {"gmail": "all_mail", "icloud": "iterate", "aol": "iterate"}

# IMAP SPECIAL-USE flags (RFC 6154) marking folders we must NEVER delete from.
# Lower-cased here so we can compare case-insensitively. "\\noselect" means the
# entry is a container, not a real mailbox you can open.
SKIP_SPECIAL_USE = (
    r"\trash",
    r"\junk",
    r"\drafts",
    r"\sent",
    r"\noselect",
    r"\all",  # only Gmail advertises \All; we handle Gmail explicitly anyway
)

# Fallback for servers (notably AOL) that don't advertise SPECIAL-USE flags:
# skip a folder if its name contains any of these substrings (case-insensitive).
SKIP_NAME_PATTERNS = ("trash", "junk", "spam", "deleted", "bin", "drafts", "sent")

# Regex to pull the flag list and mailbox name out of one raw LIST response line,
# e.g.  (\HasNoChildren \Sent) "/" "Sent Messages"
# Group 1 = the text inside the first (...) = the flags.
# Group 2 = the final token = the mailbox name (quoted or bare).
_LIST_RE = re.compile(r'^\(([^)]*)\)\s+(?:"[^"]*"|\S+)\s+(?:"([^"]*)"|(\S+))\s*$')


# --- Date helper -------------------------------------------------------------
def before_date_str(dt):
    """Format a date as an IMAP 'BEFORE' string, e.g. '01-Jan-2026'.

    NOTE: inbox_inventory has imap_since(years), but that's a *rolling* window
    (N years back from today). We need a *fixed* cutoff (Jan 1 of a given year),
    so this small helper formats an explicit date. IMAP 'BEFORE 01-Jan-2026'
    is exclusive -- it matches messages internally dated on or before
    31-Dec-2025, which is exactly "before 2026".
    """
    # :02d zero-pads the day to two digits; _MONTHS[dt.month - 1] converts the
    # 1-12 month number into the English abbreviation IMAP requires.
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def build_search_criteria(before_str, keep_from):
    """Build the IMAP SEARCH argument list.

    Returns e.g. ["BEFORE", "01-Jan-2026", "NOT", "FROM", "21131mmw@gmail.com"].
    IMAP ANDs space-separated terms together, and 'NOT FROM addr' excludes that
    sender. Caveat: IMAP FROM is a SUBSTRING match on the From header, not an
    exact address match -- so this errs on the safe side (it KEEPS anything whose
    From header merely contains the address; it never deletes it by mistake).
    """
    return ["BEFORE", before_str, "NOT", "FROM", keep_from]


# --- Folder parsing / filtering ----------------------------------------------
def parse_folder_line(line):
    """Parse one raw LIST line into (flags, name), or None if unparseable.

    `flags` is a set of lower-cased flag tokens like {"\\hasnochildren", "\\sent"};
    `name` is the mailbox name (e.g. "Sent Messages").
    """
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    flags_text = m.group(1)
    # Group 2 is the quoted name; group 3 is the bare (unquoted) name. Exactly
    # one of them matched, so we take whichever is not None.
    name = m.group(2) if m.group(2) is not None else m.group(3)
    if name is None:
        return None
    # .split() on the flag text yields the individual flag tokens; lower-case
    # them so comparisons against SKIP_SPECIAL_USE are case-insensitive.
    flags = {f.lower() for f in flags_text.split()}
    return flags, name


def is_skippable(flags, name):
    """Return (skip: bool, reason: str). Decide whether to skip a folder.

    1) Skip if it advertises a special-use flag we must never touch.
    2) Otherwise, skip if its (last path segment) name matches a known pattern
       -- a fallback for servers that don't advertise SPECIAL-USE (e.g. AOL).
    """
    # Set intersection: any special-use flag present that we want to skip?
    hit = flags & set(SKIP_SPECIAL_USE)
    if hit:
        # sorted() just makes the reason text deterministic/readable.
        return True, f"special-use {','.join(sorted(hit))}"
    # rpartition("/") splits on the LAST "/" -> (parent, "/", leaf). [-1] is the
    # leaf, so "Work/Receipts" is judged by "receipts". Handles nested folders.
    leaf = name.rpartition("/")[-1].lower()
    for pat in SKIP_NAME_PATTERNS:
        if pat in leaf:
            return True, f"name matches {pat!r}"
    return False, ""


def selectable_folders(imap):
    """Return the list of in-scope folder names, printing skips to stderr."""
    kept = []
    for line in list_folders(imap):
        parsed = parse_folder_line(line)
        if parsed is None:
            # Don't silently ignore -- tell the user we couldn't parse this line.
            print(f"    (could not parse folder line: {line!r})", file=sys.stderr)
            continue
        flags, name = parsed
        skip, reason = is_skippable(flags, name)
        if skip:
            print(f"    skip   {name}   [{reason}]", file=sys.stderr)
        else:
            print(f"    SCOPE  {name}", file=sys.stderr)
            kept.append(name)
    return kept


# --- IMAP search / fetch / delete --------------------------------------------
def search_uids(imap, criteria):
    """Return a list of matching UID byte-strings (e.g. [b'12', b'47']).

    We use UID SEARCH (not plain SEARCH) because UIDs are STABLE -- they don't
    shift when messages are expunged. Plain sequence numbers renumber on every
    expunge, which is exactly the bug you don't want in a deletion tool.
    """
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or data[0] is None:
        return []
    return data[0].split()


def fetch_detail(imap, uids, account, folder):
    """Fetch From/Date/Subject for each UID (read-only) for the audit detail log.

    Even in a delete tool we use BODY.PEEK so this fetch never marks mail as
    read. Returns a list of dict rows.
    """
    rows = []
    for i in range(0, len(uids), BATCH):
        chunk = uids[i : i + BATCH]
        uid_set = b",".join(chunk).decode()
        typ, resp = imap.uid(
            "FETCH",
            uid_set,
            "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])",
        )
        if typ != "OK":
            continue
        for item in resp:
            # imaplib mixes useful tuples with separator junk; keep only tuples
            # whose second element holds the raw header bytes.
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            # The UID is echoed in the response prefix bytes, item[0], like
            # b'12 (UID 47 BODY[HEADER...] {123}'. Pull it out with a regex.
            m = re.search(rb"UID (\d+)", item[0])
            uid = m.group(1).decode() if m else ""
            import email as _email  # local import keeps top-of-file imports lean

            msg = _email.message_from_bytes(item[1])
            name, addr = parseaddr(msg.get("From", ""))
            # msg.get("Date") is str | None; only parse when it's actually a
            # string (narrows the type for parsedate_to_datetime).
            raw_date = msg.get("Date")
            when = ""
            if raw_date:
                try:
                    dt = parsedate_to_datetime(raw_date)
                    when = dt.date().isoformat() if dt else ""
                except Exception:
                    when = ""
            rows.append(
                {
                    "account": account,
                    "folder": folder,
                    "uid": uid,
                    "date": when,
                    "from": addr.lower().strip(),
                    "subject": decode_str(msg.get("Subject", "")),
                }
            )
    return rows


def delete_uids(imap, uids):
    """Mark the given UIDs \\Deleted and EXPUNGE them. Returns the count expunged.

    This is the ONLY function that mutates a mailbox, and main() only ever calls
    it after the user has typed DELETE and the folder was opened read-write.
    """
    if not uids:
        return 0
    # Phase A: flag every candidate UID as \Deleted, in batches.
    for i in range(0, len(uids), BATCH):
        chunk = uids[i : i + BATCH]
        uid_set = b",".join(chunk).decode()
        imap.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
    # Phase B: actually remove the flagged messages.
    # UID EXPUNGE (RFC 4315) removes ONLY the UIDs we name. If the server doesn't
    # support it, fall back to plain EXPUNGE -- still safe here because the only
    # \Deleted messages in this freshly selected folder are the ones we just set.
    all_uids = b",".join(uids).decode()
    typ, _ = imap.uid("EXPUNGE", all_uids)
    if typ != "OK":
        imap.expunge()
    return len(uids)


# --- Per-provider processing -------------------------------------------------
def process_account(imap, account, criteria, do_delete, detail_log):
    """Search (and optionally delete) for one account, honoring its STRATEGY.

    Returns (report_rows, detail_rows):
      report_rows  -- one dict per folder: account/folder/candidate/deleted.
      detail_rows  -- per-message rows when detail_log is True, else [].
    """
    # Pick the folders to process based on the provider's strategy.
    if STRATEGY[account] == "all_mail":
        # Gmail: a single pass over All Mail IS the union of all mail, and is the
        # only place an expunge permanently deletes. Iterating labels would be
        # both wrong (only un-labels) and redundant.
        folders = [PROVIDERS[account]["folder"]]
    else:
        # iCloud / AOL: every normal folder (special folders already filtered).
        print(f"    discovering folders for {account}...", file=sys.stderr)
        folders = selectable_folders(imap)

    report_rows = []
    detail_rows = []
    for folder in folders:
        # readonly=True for a dry run makes mutation physically impossible;
        # readonly=False only when we're actually going to delete.
        typ, _ = imap.select(folder, readonly=not do_delete)
        if typ != "OK":
            print(f"    could not open {folder!r}; skipping", file=sys.stderr)
            continue

        uids = search_uids(imap, criteria)
        candidate = len(uids)

        if detail_log and candidate:
            detail_rows.extend(fetch_detail(imap, uids, account, folder))

        deleted = 0
        if do_delete and candidate:
            deleted = delete_uids(imap, uids)

        verb = "deleted" if do_delete else "would delete"
        print(
            f"    {account} :: {folder}  ->  {verb} {candidate}", file=sys.stderr
        )
        report_rows.append(
            {
                "account": account,
                "folder": folder,
                "candidate_count": candidate,
                "deleted_count": deleted,
            }
        )
    return report_rows, detail_rows


# --- CSV writers -------------------------------------------------------------
def write_audit_csv(path, rows, dry_run):
    """Write the summary audit CSV (one row per account/folder)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["account", "folder", "candidate_count", "deleted_count", "dry_run"]
        )
        for r in rows:
            w.writerow(
                [
                    r["account"],
                    r["folder"],
                    r["candidate_count"],
                    r["deleted_count"],
                    "yes" if dry_run else "",
                ]
            )


def write_detail_csv(path, rows):
    """Write the per-message detail CSV (only when --detail-log is set)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account", "folder", "uid", "date", "from", "subject"])
        for r in rows:
            w.writerow(
                [r["account"], r["folder"], r["uid"], r["date"], r["from"], r["subject"]]
            )


# --- Confirmation safeguard --------------------------------------------------
def confirm_prompt(report_rows, before_str, keep_from):
    """Show a big summary of the pending PERMANENT delete and require typed DELETE.

    Returns True only if the user types exactly 'DELETE'.
    """
    total = sum(r["candidate_count"] for r in report_rows)
    print("\n" + "=" * 64, file=sys.stderr)
    print("  PERMANENT DELETE -- this cannot be undone (no Trash recovery)", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print(f"  Cutoff      : delete mail BEFORE {before_str}", file=sys.stderr)
    print(f"  Keeping     : mail from {keep_from}", file=sys.stderr)
    print(f"  Candidates  : {total} messages across {len(report_rows)} folder(s)", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    # input() (not getpass) -- we WANT this typed word visible for confirmation.
    answer = input("  Type DELETE to permanently delete, anything else to abort: ")
    return answer.strip() == "DELETE"


# --- Main orchestration ------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Permanently delete old mail across mailboxes (dry-run by default)."
    )
    ap.add_argument(
        "--accounts",
        nargs="+",
        required=True,
        choices=list(PROVIDERS),
        help="Which mailboxes to clean (gmail icloud aol).",
    )
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the script is a dry run.",
    )
    ap.add_argument(
        "--before",
        metavar="YYYY-MM-DD",
        help="Delete mail before this date (default: Jan 1 of the current year).",
    )
    ap.add_argument(
        "--keep-from",
        default=DEFAULT_KEEP_FROM,
        help=f"Never delete mail from this sender (default: {DEFAULT_KEEP_FROM}).",
    )
    ap.add_argument(
        "--detail-log",
        action="store_true",
        help="Also write cleanup_detail.csv listing every candidate message.",
    )
    ap.add_argument(
        "--list-folders",
        action="store_true",
        help="Show which folders are in scope vs skipped, then exit.",
    )
    ap.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file with credentials (default: .env).",
    )
    ap.add_argument(
        "--outdir",
        default=".",
        help="Directory to write the audit CSV(s) into (default: current).",
    )
    args = ap.parse_args()

    load_env(args.env_file)
    outdir = args.outdir.rstrip("/")

    # Work out the cutoff date. Default = Jan 1 of the current year.
    if args.before:
        try:
            cutoff = datetime.strptime(args.before, "%Y-%m-%d")
        except ValueError:
            print(f"Bad --before date {args.before!r}; use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(2)  # exit code 2 = usage error, by convention.
    else:
        today = datetime.now()
        cutoff = datetime(today.year, 1, 1)
    before_str = before_date_str(cutoff)
    criteria = build_search_criteria(before_str, args.keep_from)

    # Loud banner so the mode is never ambiguous.
    mode = "DELETE (live)" if args.confirm else "DRY RUN (no changes)"
    print(f"\nMode: {mode}", file=sys.stderr)
    print(f"Deleting mail BEFORE {before_str}, keeping {args.keep_from}\n", file=sys.stderr)

    # PHASE 1 -- always COUNT first (read-only), even when --confirm is set, so
    # the user sees real numbers before any deletion happens.
    all_report = []
    all_detail = []
    connections = {}  # keep connections open to reuse in the delete phase
    for account in args.accounts:
        user, password = get_credentials(account)
        try:
            imap = connect(account, user, password)
        except imaplib.IMAP4.error as e:
            print(f"  login failed for {account}: {e}", file=sys.stderr)
            print("  -> use an APP-SPECIFIC password and enable IMAP.", file=sys.stderr)
            continue

        # --list-folders short-circuit: just show scope and move on.
        if args.list_folders:
            print(f"\nFolders for {account}:", file=sys.stderr)
            if STRATEGY[account] == "all_mail":
                print(f"    SCOPE  {PROVIDERS[account]['folder']} (Gmail: All Mail only)", file=sys.stderr)
            else:
                selectable_folders(imap)
            imap.logout()
            continue

        print(f"  scanning {account} ...", file=sys.stderr)
        try:
            # do_delete=False here: PHASE 1 is always a read-only count.
            rows, detail = process_account(
                imap, account, criteria, do_delete=False, detail_log=args.detail_log
            )
        except Exception as e:
            print(f"  scan failed for {account}: {e}", file=sys.stderr)
            imap.logout()
            continue

        all_report.extend(rows)
        all_detail.extend(detail)
        connections[account] = imap  # keep open for a possible delete phase

    if args.list_folders:
        return  # nothing else to do in list-folders mode.

    # Write the dry-run audit + detail now, so there is always a record on disk.
    audit_path = f"{outdir}/cleanup_audit.csv"
    write_audit_csv(audit_path, all_report, dry_run=not args.confirm)
    print(f"\nAudit written: {audit_path}", file=sys.stderr)
    if args.detail_log:
        detail_path = f"{outdir}/cleanup_detail.csv"
        write_detail_csv(detail_path, all_detail)
        print(f"Detail written: {detail_path}", file=sys.stderr)

    total = sum(r["candidate_count"] for r in all_report)

    # If this is a dry run, stop here -- nothing was (or could be) deleted.
    if not args.confirm:
        print(f"\nDRY RUN complete: {total} message(s) WOULD be deleted.", file=sys.stderr)
        print("Re-run with --confirm to actually delete.", file=sys.stderr)
        for imap in connections.values():
            imap.logout()
        return

    # --confirm path: nothing to do if there are no candidates.
    if total == 0:
        print("\nNothing to delete.", file=sys.stderr)
        for imap in connections.values():
            imap.logout()
        return

    # PHASE 2 -- require the typed DELETE confirmation, then actually delete.
    if not confirm_prompt(all_report, before_str, args.keep_from):
        print("\nAborted. No changes made.", file=sys.stderr)
        for imap in connections.values():
            imap.logout()
        return

    print("\nDeleting...", file=sys.stderr)
    final_report = []
    for account, imap in connections.items():
        try:
            # do_delete=True: now folders open read-write and we expunge.
            rows, _ = process_account(
                imap, account, criteria, do_delete=True, detail_log=False
            )
            final_report.extend(rows)
        except Exception as e:
            print(f"  delete failed for {account}: {e}", file=sys.stderr)
        finally:
            imap.logout()

    # Overwrite the audit with the real deleted counts.
    write_audit_csv(audit_path, final_report, dry_run=False)
    deleted = sum(r["deleted_count"] for r in final_report)
    print(f"\nDone. Permanently deleted {deleted} message(s).", file=sys.stderr)
    print(f"Final audit written: {audit_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
