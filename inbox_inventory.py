#!/usr/bin/env python3
# ^ This first line is called a "shebang". On macOS/Linux it lets you run the
#   file directly (./inbox_inventory.py) and the OS will use python3 to execute
#   it. `/usr/bin/env python3` means "find python3 on the user's PATH".
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
# ^ The triple-quoted string directly under the shebang is the "module
#   docstring". Python stores it as the module's __doc__ attribute, and tools
#   (and `help(inbox_inventory)`) display it. It is the human-readable contract
#   for the whole file -- read this before changing behavior.

# --- Imports -----------------------------------------------------------------
# `import x` makes the module `x` available as a name. Everything here is from
# Python's STANDARD LIBRARY, which means no `pip install` is ever needed.
import argparse  # parse command-line flags like --accounts, --limit
import csv  # read/write CSV (comma-separated values) files
import email  # parse raw email bytes into a structured Message object
import getpass  # read a password from the terminal WITHOUT echoing it
import imaplib  # talk to mail servers over IMAP (the protocol email clients use)
import os  # interact with the operating system (env vars, file paths)
import re  # regular expressions (pattern matching in text)
import sys  # access to sys.stderr, sys.argv, etc.

# `from module import name` pulls a single name in so you can use it directly
# (e.g. `defaultdict(...)` instead of `collections.defaultdict(...)`).
from collections import defaultdict  # a dict that auto-creates missing entries
from datetime import datetime, timedelta  # date math
from email.header import decode_header, make_header  # decode "=?UTF-8?..." headers
from email.utils import parseaddr, parsedate_to_datetime  # parse From/Date headers


# --- Configuration constants -------------------------------------------------
# CONSTANTS are written in UPPER_CASE by convention. Python does not actually
# enforce immutability; the naming is a signal to other programmers "don't
# reassign this".

# A dict (dictionary) maps keys to values. Here each provider name maps to a
# nested dict of connection settings. Nested dicts are a lightweight way to keep
# related config together -- this is "table-driven" design: add a provider by
# adding one entry, no other code changes needed.
PROVIDERS = {
    "gmail": {"host": "imap.gmail.com", "port": 993, "folder": '"[Gmail]/All Mail"'},
    "icloud": {"host": "imap.mail.me.com", "port": 993, "folder": "INBOX"},
    "aol": {"host": "imap.aol.com", "port": 993, "folder": "INBOX"},
}
# Note: Gmail's "All Mail" is everything you've ever received (most complete,
# but large/slow); override with --folder INBOX or narrow with --since.

# A tuple is like a list but immutable (cannot be changed after creation).
# Tuples are a good fit for a fixed set of constants like these keywords.
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

# The IMAP "fetch" instruction. BODY.PEEK is the magic part: PEEK means "read
# the headers WITHOUT marking the message as seen/read". HEADER.FIELDS limits
# the download to just these five headers -- we never pull the message body.
FETCH_FIELDS = "BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT LIST-UNSUBSCRIBE LIST-ID)]"
BATCH = 500  # messages fetched per server round-trip (fewer round-trips = faster)

# A list of the English month abbreviations. The IMAP SINCE search command
# requires this exact English format regardless of the computer's locale.
# Lists use [] and ARE mutable (unlike the tuple above).
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
# A leading underscore (_MONTHS, _ANGLE) is a convention meaning "private/
# internal -- not part of this module's public interface".

# re.compile builds a reusable regular-expression object once, which is more
# efficient than re-parsing the pattern on every call. The raw string r"..."
# stops Python from treating backslashes specially, which keeps regex readable.
# This pattern matches text wrapped in angle brackets: <...>. The parentheses
# create a "capture group" so we can extract just the inside part.
_ANGLE = re.compile(r"<([^>]+)>")


# --- Helper functions --------------------------------------------------------
# `def name(args):` defines a function. The indented block below is its body.

def decode_str(raw):
    """Decode a possibly MIME-encoded header (=?UTF-8?...?=) to plain text."""
    # ^ This one-line string is the function's docstring -- it documents what
    #   the function does and shows up in help(decode_str).
    if not raw:
        # `not raw` is True for None and for the empty string "" (both are
        # "falsy" in Python). Guarding against empty input up front is a common
        # pattern called an "early return".
        return ""
    try:
        # try/except handles errors gracefully. If decoding raises ANY
        # exception, we fall through to `except` instead of crashing.
        # Email headers can be encoded like "=?UTF-8?B?...?="; make_header +
        # decode_header turn that back into normal readable text.
        return str(make_header(decode_header(raw)))
    except Exception:
        # If we couldn't decode it, returning the original is better than dying.
        return raw


def extract_unsub(header_value):
    """Pull the best unsubscribe target out of a List-Unsubscribe header.

    Header looks like: <https://x.com/unsub?id=1>, <mailto:unsub@x.com?subject=no>
    Prefer an http(s) link (clickable); fall back to mailto; else raw.
    """
    if not header_value:
        return ""
    # Some servers (e.g. iCloud) hand back this header as an email.header.Header
    # object instead of a plain str. The regex below only accepts str/bytes, so
    # coerce first -- str(Header) yields the decoded header text. Without this,
    # the regex raises "expected string or bytes-like object, got 'Header'".
    header_value = str(header_value)
    # _ANGLE.findall returns a LIST of every bracketed value, e.g.
    # ["https://x.com/unsub?id=1", "mailto:unsub@x.com?subject=no"].
    # The `or [...]` provides a fallback: if findall finds nothing (empty list,
    # which is falsy), use the whole header value stripped of whitespace.
    links = _ANGLE.findall(header_value) or [header_value.strip()]
    # This is a "list comprehension": build a new list by looping over `links`
    # and keeping only items where the condition is True. It reads as:
    # "link for each link in links if link (lowercased) starts with 'http'".
    # (Single-letter names like `l` are avoided -- they read like 1 or I; the
    #  linter flags them as ambiguous, Ruff rule E741.)
    http = [link for link in links if link.lower().startswith("http")]
    if http:
        return http[0]  # [0] is the first element; lists are zero-indexed.
    mailto = [link for link in links if link.lower().startswith("mailto:")]
    if mailto:
        return mailto[0]
    # A "ternary" / conditional expression: VALUE_IF_TRUE if CONDITION else
    # VALUE_IF_FALSE. Returns the first link, or "" if the list is empty.
    return links[0] if links else ""


def imap_since(years):
    """Return an IMAP SINCE date string (e.g. '02-Jun-2023') N years back."""
    # datetime.now() is "right now". timedelta is a span of time you can
    # subtract from a date. 365.25 accounts for leap years on average.
    cutoff = datetime.now() - timedelta(days=round(365.25 * years))
    # An f-string (formatted string literal, prefixed with f) lets you embed
    # expressions inside {} directly in the text. ":02d" is a FORMAT SPEC
    # meaning "integer, zero-padded to 2 digits" so 2 becomes "02".
    # _MONTHS[cutoff.month - 1] -- months are 1-12 but list indexes are 0-11,
    # hence the - 1.
    return f"{cutoff.day:02d}-{_MONTHS[cutoff.month - 1]}-{cutoff.year}"


def load_env(path=".env"):
    """Minimal .env loader (no dependencies). Lines look like KEY=value;
    blank lines and #comments are ignored, surrounding quotes are stripped.
    Real environment variables already set take precedence (never overridden).
    """
    # `path=".env"` is a DEFAULT ARGUMENT: if the caller does not pass `path`,
    # it defaults to ".env".
    if not os.path.exists(path):
        return  # nothing to do if the file isn't there; just return (None).
    # `with open(...) as f:` is a CONTEXT MANAGER. It guarantees the file is
    # closed automatically when the block ends, even if an error occurs.
    with open(path, encoding="utf-8") as f:
        # Iterating over a file object yields one line at a time (memory-cheap
        # even for huge files).
        for line in f:
            line = line.strip()  # remove leading/trailing whitespace + newline
            # Skip blank lines, comments (#...), and lines without an '='.
            if not line or line.startswith("#") or "=" not in line:
                continue  # `continue` jumps to the next loop iteration.
            if line.startswith("export "):
                # Allow `export KEY=value` (shell syntax) by slicing off the
                # prefix. len("export ") computes the prefix length so the slice
                # stays correct if you ever edit the literal.
                line = line[len("export ") :]
            # str.partition splits on the FIRST '=' into (before, '=', after).
            # We ignore the middle '=' by assigning it to `_` (a throwaway name).
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            # If the value is wrapped in matching quotes, strip them. Checks:
            # at least 2 chars, first char == last char, and it's a quote.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]  # slice off the first and last characters.
            # setdefault only sets the key if it is NOT already present, so a
            # real environment variable always wins over the .env file value.
            os.environ.setdefault(key, val)  # don't clobber real env vars


def get_credentials(account):
    """Resolve credentials from env/.env, prompting for anything missing."""
    prefix = account.upper()  # e.g. "gmail" -> "GMAIL"
    # os.environ is a dict-like view of environment variables. .get(key, "")
    # returns "" if the key is missing instead of raising a KeyError.
    user = os.environ.get(f"{prefix}_EMAIL", "").strip()
    password = os.environ.get(f"{prefix}_APP_PASSWORD", "")
    if not user:
        # input() prints the prompt and reads a line the user types (visible).
        user = input(f"[{account}] email address: ").strip()
    if not password:
        # getpass reads input WITHOUT showing it on screen -- correct for secrets.
        password = getpass.getpass(f"[{account}] APP-SPECIFIC password: ")
    # Returning multiple values actually returns a single tuple (user, password).
    # The caller can unpack it: `user, password = get_credentials(...)`.
    return user, password


def connect(provider, user, password):
    cfg = PROVIDERS[provider]  # look up this provider's host/port/folder dict.
    # IMAP4_SSL opens an encrypted (SSL/TLS) connection to the mail server.
    imap = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    imap.login(user, password)  # authenticate; raises on bad credentials.
    return imap  # hand the live connection object back to the caller.


def list_folders(imap):
    # imaplib methods conventionally return a (type, data) pair. `typ` is "OK"
    # or "NO"; `data` is the payload. We capture both even though we ignore typ.
    typ, data = imap.list()
    out = []  # start with an empty list and append to it as we go.
    # `data or []` guards against data being None (avoids iterating over None).
    for line in data or []:
        # The server sends bytes, not str. isinstance checks the type before we
        # try to decode, which avoids errors on unexpected entries.
        if isinstance(line, bytes):
            # .decode turns bytes into a str; errors="replace" swaps any
            # undecodable byte for a placeholder instead of crashing.
            out.append(line.decode(errors="replace"))
    return out


def parse_message(raw_bytes):
    # Turn the raw header bytes the server sent into a Message object we can
    # query by header name with .get(...).
    msg = email.message_from_bytes(raw_bytes)
    # parseaddr splits a "From" header like 'Jane Doe <jane@x.com>' into the
    # tuple ("Jane Doe", "jane@x.com"). We unpack it into name and addr.
    name, addr = parseaddr(msg.get("From", ""))
    addr = addr.lower().strip()  # normalize so "Jane@X.com" == "jane@x.com".
    # The domain is everything after the last '@'. split("@") returns a list;
    # [-1] is its last element (negative indexes count from the end).
    # If there's no '@', fall back to the address itself or "(unknown)".
    domain = addr.split("@")[-1] if "@" in addr else (addr or "(unknown)")
    subject = decode_str(msg.get("Subject", "")).lower()
    when = None  # default; we'll fill it in if the Date header parses.
    # msg.get("Date") is `str | None`. parsedate_to_datetime wants a `str`, so
    # we narrow the type by only calling it when the header is actually present
    # (this also silences the type checker's "str | None not assignable" warning).
    raw_date = msg.get("Date")
    if raw_date:
        try:
            # parsedate_to_datetime converts an email Date header into a datetime.
            dt = parsedate_to_datetime(raw_date)
            if dt:
                # .date() drops the time; .isoformat() gives "2024-01-31".
                when = dt.date().isoformat()
        except Exception:
            # Malformed dates are common in real mail -- don't crash on them.
            when = None
    unsub_raw = msg.get("List-Unsubscribe")
    # Build and return a dict describing this one message. Returning a dict (vs.
    # many separate values) keeps the caller readable: info["domain"], etc.
    return {
        "name": decode_str(name),
        "address": addr,
        "domain": domain,
        "date": when,
        # `is not None` checks presence of the header. The whole expression is a
        # boolean: True if either List-Unsubscribe OR List-Id was present.
        "has_unsub": unsub_raw is not None or msg.get("List-Id") is not None,
        "unsub_link": extract_unsub(unsub_raw),
        # any(...) returns True if AT LEAST ONE item in the iterable is truthy.
        # Here: True if any signup keyword appears anywhere in the subject.
        "is_signup": any(k in subject for k in SIGNUP_KEYWORDS),
    }


def scan(imap, folder, limit=None, since_years=None):
    # readonly=True is the hard safety guarantee: the server will refuse to let
    # us modify anything (no marking-as-read, no deletes) in this session.
    typ, _ = imap.select(folder, readonly=True)
    if typ != "OK":
        # `raise` throws an exception. The f-string with {folder!r} uses !r to
        # show the repr() of the value (with quotes), helpful for debugging.
        raise RuntimeError(
            f"Could not open folder {folder!r}. "
            f"Run with --list-folders to see valid folder names."
        )

    if since_years:
        date_str = imap_since(since_years)
        # file=sys.stderr sends this to the "standard error" stream, separate
        # from normal output -- so progress text doesn't pollute piped output.
        print(f"    (only messages since {date_str})", file=sys.stderr)
        # Ask the server to return only message ids newer than date_str.
        typ, data = imap.search(None, "SINCE", date_str)
    else:
        typ, data = imap.search(None, "ALL")  # every message id in the folder.

    # search returns ids as one space-separated bytes blob; .split() makes a
    # list of individual id byte-strings, e.g. [b"1", b"2", b"3"].
    ids = data[0].split()
    if limit:
        # SLICING: ids[-limit:] keeps the LAST `limit` elements. IMAP ids grow
        # over time, so the highest ids are the most recent messages.
        ids = ids[-limit:]  # most recent N (highest sequence numbers)
    total = len(ids)
    if total == 0:
        return {}  # nothing to scan; return an empty dict.

    # defaultdict auto-creates a value for any key you access that doesn't exist
    # yet, using the factory function you pass in. Here the factory is a lambda
    # (a tiny anonymous function) that returns a fresh per-domain record dict.
    # This is why `agg[info["domain"]]` below never raises KeyError.
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

    done = 0  # running counter for the progress display.
    # range(start, stop, step) yields 0, 500, 1000, ... up to `total`. We walk
    # the ids in chunks of BATCH to minimize slow network round-trips.
    for i in range(0, total, BATCH):
        chunk = ids[i : i + BATCH]  # slice out up to BATCH ids for this round.
        # The server wants a comma-joined id string like "1,2,3". chunk holds
        # bytes, so we join with a bytes separator b"," then .decode() to str.
        msg_set = b",".join(chunk).decode()
        # Fetch only our header fields for this whole chunk in one request.
        typ, resp = imap.fetch(msg_set, f"({FETCH_FIELDS})")
        for item in resp:
            # imaplib mixes useful (tuple) items with separator junk. We only
            # want tuples whose second element (item[1]) holds the raw header
            # bytes. This guard skips everything else safely.
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            info = parse_message(item[1])
            # Accessing agg[domain] AUTO-CREATES the record on first sight
            # (defaultdict magic). `rec` is a REFERENCE to the dict inside agg,
            # so mutating rec[...] below updates the stored record in place.
            rec = agg[info["domain"]]
            rec["count"] += 1  # += 1 is shorthand for rec["count"] = ... + 1.
            # Fill name/address once (the first non-empty value we encounter).
            if not rec["name"] and info["name"]:
                rec["name"] = info["name"]
            if not rec["address"] and info["address"]:
                rec["address"] = info["address"]
            d = info["date"]
            if d:
                # Track the earliest (first) and latest (last) dates seen.
                # ISO date strings like "2024-01-31" compare correctly with <
                # and > because the format is year-month-day, biggest unit first.
                if rec["first"] is None or d < rec["first"]:
                    rec["first"] = d
                if rec["last"] is None or d > rec["last"]:
                    rec["last"] = d
            # `a = a or b` keeps a True once it has ever been True ("sticky OR").
            rec["mailing_list"] = rec["mailing_list"] or info["has_unsub"]
            rec["likely_account"] = rec["likely_account"] or info["is_signup"]
            # keep the unsubscribe link from the most recent message that had one
            link = info["unsub_link"]
            if link:
                if not rec["unsub_link"]:
                    # First link we've seen for this domain -- just take it.
                    rec["unsub_link"] = link
                    rec["unsub_link_date"] = d
                elif d and (
                    # We already have a link; replace it only if THIS message is
                    # newer (or the stored one had no date to compare against).
                    rec["unsub_link_date"] is None or d > rec["unsub_link_date"]
                ):
                    rec["unsub_link"] = link
                    rec["unsub_link_date"] = d
        done += len(chunk)
        # end="\r" prints a carriage return (no newline), so each progress line
        # overwrites the previous one on the same terminal row.
        print(f"    {done}/{total} messages scanned", file=sys.stderr, end="\r")
    print(file=sys.stderr)  # final newline so the next output starts cleanly.
    return agg


def write_account_csv(path, agg):
    # sorted(...) returns a new sorted list. agg.items() yields (key, value)
    # pairs. `key=lambda kv: kv[1]["count"]` sorts by each pair's value-dict
    # "count"; reverse=True puts the biggest senders first.
    rows = sorted(agg.items(), key=lambda kv: kv[1]["count"], reverse=True)
    # newline="" is REQUIRED by the csv module to avoid blank lines on Windows.
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)  # a writer object that formats rows as CSV.
        w.writerow(  # write the single header row first.
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
        for domain, r in rows:  # unpack each (domain, record) pair.
            w.writerow(
                [
                    domain,
                    r["name"],
                    r["address"],
                    r["count"],
                    # `r["first"] or ""` turns a None into an empty cell.
                    r["first"] or "",
                    r["last"] or "",
                    # Convert booleans into friendly "yes"/blank CSV cells.
                    "yes" if r["mailing_list"] else "",
                    "yes" if r["likely_account"] else "",
                    r["unsub_link"],
                ]
            )


def write_worklist(path, per_account):
    # per_account is a dict mapping account name -> its aggregated agg dict.
    # This writes ONE combined CSV across all accounts, with extra blank columns
    # the user fills in by hand to drive their cleanup decisions.
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
        # Outer loop over accounts, inner loop over each account's senders.
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
                )  # the four trailing "" cells are action/new_email/migrated/notes


def main():
    # argparse builds a command-line interface for you: it parses sys.argv,
    # validates types/choices, and auto-generates --help text.
    ap = argparse.ArgumentParser(
        description="Inventory senders across mailboxes and export to CSV."
    )
    ap.add_argument(
        "--accounts",
        nargs="+",  # accept ONE OR MORE values: --accounts gmail icloud
        required=True,  # the program errors out if this flag is missing.
        choices=list(PROVIDERS),  # only allow the keys we defined in PROVIDERS.
        help="Which mailboxes to scan (gmail icloud aol).",
    )
    ap.add_argument(
        "--folder", help="Override the folder to scan (applies to all accounts)."
    )
    ap.add_argument(
        "--limit",
        type=int,  # argparse converts the text to an int automatically.
        help="Only scan the most recent N messages (good for testing).",
    )
    ap.add_argument(
        "--since",
        type=float,
        metavar="YEARS",  # the placeholder name shown in --help.
        help="Only scan mail from the last N years (server-side, faster).",
    )
    ap.add_argument(
        "--list-folders",
        action="store_true",  # a flag: present -> True, absent -> False.
        help="List folders for the chosen accounts and exit.",
    )
    ap.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file with credentials (default: .env).",
    )
    ap.add_argument(
        "--outdir",
        default=".",  # "." means the current working directory.
        help="Directory to write CSV files into (default: current).",
    )
    args = ap.parse_args()  # do the parsing; `args` holds the results.

    load_env(args.env_file)  # populate os.environ from the .env file (if any).
    outdir = args.outdir.rstrip("/")  # drop a trailing slash for clean paths.
    per_account = {}  # will collect each account's results for the worklist.

    for account in args.accounts:
        user, password = get_credentials(account)  # tuple-unpacking again.
        try:
            imap = connect(account, user, password)
        # `except SpecificError as e:` catches just login errors and binds the
        # exception object to `e` so we can print its message.
        except imaplib.IMAP4.error as e:
            print(f"  login failed for {account}: {e}", file=sys.stderr)
            print(
                "  -> use an APP-SPECIFIC password and make sure IMAP is enabled.",
                file=sys.stderr,
            )
            continue  # skip this account and move on to the next one.

        if args.list_folders:
            # When the user only wants folder names, print them and skip scanning.
            print(f"\nFolders for {account}:")
            for fol in list_folders(imap):
                print("  ", fol)
            imap.logout()
            continue

        # `a or b` returns `a` if it's truthy, else `b` -- so a --folder override
        # wins, otherwise we use this provider's default folder.
        folder = args.folder or PROVIDERS[account]["folder"]
        print(f"  scanning {account} :: {folder} ...", file=sys.stderr)
        try:
            agg = scan(imap, folder, limit=args.limit, since_years=args.since)
        except Exception as e:
            # Catch-all so one bad account doesn't kill the whole run.
            print(f"  scan failed for {account}: {e}", file=sys.stderr)
            imap.logout()
            continue

        if agg:  # an empty dict is falsy, so this is "if we found anything".
            per_account[account] = agg
            out = f"{outdir}/senders_{account}.csv"
            write_account_csv(out, agg)
            print(f"  wrote {out}  ({len(agg)} unique domains)", file=sys.stderr)
        else:
            print(f"  no messages found for {account}", file=sys.stderr)
        imap.logout()  # always close the connection politely.

    # Only write the combined file if we actually gathered data.
    if per_account and not args.list_folders:
        wl = f"{outdir}/worklist.csv"
        write_worklist(wl, per_account)
        print(f"\nCombined worklist written: {wl}", file=sys.stderr)


# This guard means "only run main() when this file is executed directly"
# (python3 inbox_inventory.py). If another file imports this module instead,
# __name__ won't be "__main__", so main() won't auto-run -- letting the module
# be reused as a library. This is one of the most common Python idioms.
if __name__ == "__main__":
    main()
