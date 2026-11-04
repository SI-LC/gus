"""Read job-search emails and calendar invites from one Gmail account.

Uses IMAP with a Google app password, set in the cockpit (Settings → Email). The mailbox is
opened read-only and messages are never marked as read. Only senders and subjects are read
first; a message's body is fetched only if it matches your filters, and only a short snippet
is kept. Emails go to data/emails.json, which only the offline inbox agent can read: agents
with internet access are denied it. Agents never see the mailbox or the password.

    python3 -m radar.inbox           # fetch new matching emails
    python3 -m radar.inbox --test    # check the login and count matches, saving nothing
"""
from __future__ import annotations

import email
import imaplib
import re
import sys
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from radar.calendar_invites import is_invite, keep
from radar.scan import DATA, load_config, strip_html
from radar.settings import secret
from radar.store import locked

EMAILS = DATA / "emails.json"
HOST = "imap.gmail.com"
MAX_MESSAGES = 300  # newest first, per run
SNIPPET = 1500


def header(value) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except (ValueError, LookupError):
        return str(value or "").strip()


def matches(sender: str, subject: str, senders: list[str], subjects) -> bool:
    addr = parseaddr(sender)[1].lower()
    return any(s in addr for s in senders) or bool(subjects and subjects.search(subject))


def body_text(msg) -> str:
    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except (AttributeError, LookupError):
            continue
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not html:
            html = text
    return re.sub(r"\s+", " ", plain or strip_html(html)).strip()


def invites_in(msg) -> list[dict]:
    from radar.calendar_invites import parse_ics
    out = []
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "text/calendar" or name.endswith(".ics"):
            try:
                out += parse_ics(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except (AttributeError, LookupError):
                continue
    return out


def rules(cfg):
    senders = [s.strip().lower() for s in re.split(r"[\n,]", cfg.get("inbox_senders") or "") if s.strip()]
    pattern = cfg.get("inbox_subjects") or ""
    return senders, re.compile(pattern, re.I) if pattern else None


def connect(cfg):
    address, password = (cfg.get("gmail_address") or "").strip(), secret("gmail_app_password").replace(" ", "")
    if not (address and password):
        raise ValueError("Add your Gmail address and app password in Settings → Email")
    box = imaplib.IMAP4_SSL(HOST, timeout=30)
    box.login(address, password)
    box.select("INBOX", readonly=True)  # read-only: nothing can be changed or marked as read
    return box


def fetch(box, cfg) -> list[dict]:
    senders, subjects = rules(cfg)
    since = (date.today() - timedelta(days=int(cfg.get("inbox_days") or 3))).strftime("%d-%b-%Y")
    ok, data = box.search(None, "SINCE", since)
    if ok != "OK":
        raise RuntimeError("Gmail search failed")
    found = []
    for num in data[0].split()[-MAX_MESSAGES:][::-1]:
        ok, parts = box.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
        if ok != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        head = email.message_from_bytes(parts[0][1])
        sender, subject = header(head.get("From")), header(head.get("Subject"))
        invite = is_invite(sender, subject)
        if not invite and not matches(sender, subject, senders, subjects):
            continue  # not opened
        found.append({"num": num, "id": head.get("Message-ID", "").strip() or f"{sender}|{subject}", "invite": invite,
                      "sender": sender, "subject": subject, "date": head.get("Date")})
    return found


def save(box, found) -> int:
    added = 0
    invites = []
    with locked(EMAILS) as emails:
        for m in found:
            key = f"email:{m['id']}"
            if key in emails:
                continue
            ok, parts = box.fetch(m["num"], "(BODY.PEEK[])")
            if ok != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            if m["invite"]:
                events = invites_in(msg)
                invites += events
                emails[key] = {"status": "invite"}  # remembered so it isn't opened again; no content kept
                if events or not matches(m["sender"], m["subject"], *rules(load_config())):
                    continue
            try:
                when = parsedate_to_datetime(m["date"]).isoformat()
            except (TypeError, ValueError):
                when = datetime.now().isoformat(timespec="seconds")
            emails[key] = {"source": "email", "kind": "email", "title": m["subject"], "sender": m["sender"],
                           "text": body_text(msg)[:SNIPPET], "date": when, "status": "new"}
            added += 1
    return added, keep(invites)


def test() -> dict:
    """Used by the cockpit: proves the login works, and saves nothing."""
    cfg = load_config()
    try:
        box = connect(cfg)
        try:
            n = len(fetch(box, cfg))
        finally:
            box.logout()
        return {"ok": True, "reply": f"Connected. {n} matching emails in the last {cfg.get('inbox_days') or 3} days"}
    except imaplib.IMAP4.error:
        return {"ok": False, "reply": "Gmail refused the login. Check the address, and that this is an app password"}
    except Exception as e:
        return {"ok": False, "reply": str(e)[:200]}


def main():
    cfg = load_config()
    if "--test" in sys.argv:
        print(test())
        return
    if not cfg.get("source_gmail"):
        return
    try:
        box = connect(cfg)
        try:
            added, meetings = save(box, fetch(box, cfg))
            print(f"inbox: {added} new emails, {meetings} new meetings")
        finally:
            box.logout()
    except Exception as e:  # never print server replies: they can echo the login
        print(f"inbox: failed ({type(e).__name__})")


if __name__ == "__main__":
    main()
