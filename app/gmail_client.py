"""Read-only Gmail access via the official API (OAuth). No screen-scraping."""
from __future__ import annotations

import base64
import re
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator

from . import config
from .parsers import parse_html

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if config.GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(config.GMAIL_TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not config.GMAIL_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"{config.GMAIL_CREDENTIALS_FILE.name} not found - see README step 'Connect Gmail'"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(config.GMAIL_CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        config.GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _walk(part: dict) -> Iterator[dict]:
    yield part
    for p in part.get("parts", []) or []:
        yield from _walk(p)


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _meta(msg_id: str, headers: list[dict]) -> dict:
    from email.utils import parseaddr

    try:
        received = parsedate_to_datetime(_header(headers, "Date")).isoformat()
    except Exception:
        received = None
    raw_from = _header(headers, "From")
    return {
        "source_key": f"gmail:{msg_id}",
        "message_id": msg_id,
        "sender": re.sub(r"\s*<.*>", "", raw_from).strip('" ') or raw_from,
        "sender_email": parseaddr(raw_from)[1].lower(),
        "subject": _header(headers, "Subject"),
        "received_at": received,
    }


def _split(msg: dict) -> tuple[list[tuple[str, dict]], str, str]:
    """Attachment parts (name, body-ref) plus the plain / html bodies. Downloads nothing."""
    att_parts: list[tuple[str, dict]] = []
    plain, html = "", ""
    for part in _walk(msg["payload"]):
        fname, body = part.get("filename"), part.get("body", {})
        if fname and (body.get("attachmentId") or body.get("data")):
            att_parts.append((fname, body))
        elif part.get("mimeType") == "text/plain" and body.get("data"):
            plain += _b64(body["data"]).decode("utf-8", errors="replace")
        elif part.get("mimeType") == "text/html" and body.get("data"):
            html += _b64(body["data"]).decode("utf-8", errors="replace")
    return att_parts, plain, html


def _download(svc, msg_id: str, source_key: str, att_parts, already_seen) -> list[tuple[str, bytes]]:
    atts: list[tuple[str, bytes]] = []
    for fname, body in att_parts:
        if already_seen(f"{source_key}:{fname}"):
            continue
        if body.get("attachmentId"):
            a = svc.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=body["attachmentId"]).execute()
            atts.append((fname, _b64(a["data"])))
        else:
            atts.append((fname, _b64(body["data"])))
    return atts


def fetch_new(
    message_known: Callable[[str], bool],
    already_seen: Callable[[str], bool],
    rules,
    on_ignored: Callable[[dict, str], None],
) -> Iterator[tuple[dict, list[tuple[str, bytes]], str]]:
    """Yield (meta, [(filename, bytes)], body_text) for each WANTED email not yet processed.

    Cost control, in order:  1) messages already handled are skipped without any call,
    2) sender/subject are checked from a headers-only fetch, 3) mail with no attachment that still fails
    the rules is dropped from those headers (never opened), 4) only the rest is fetched in full,
    5) attachments are downloaded only after the attachment rules pass.
    Unwanted mail is reported through on_ignored(meta, reason) and never read.
    """
    from . import filters

    svc = _service()
    query = filters.gmail_query(rules, config.GMAIL_QUERY)   # lets Gmail itself drop the obvious mail
    with_att: set[str] | None = None

    def _ids_with_attachments() -> set[str]:
        """One extra (cheap, ids-only) search so mail WITHOUT attachments can be judged from its
        headers alone and never has to be opened."""
        ids: set[str] = set()
        tok = None
        while True:
            r = svc.users().messages().list(userId="me", q=f"{query} has:attachment", maxResults=500,
                                            pageToken=tok).execute()
            ids.update(m["id"] for m in r.get("messages", []))
            tok = r.get("nextPageToken")
            if not tok:
                return ids

    page = None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=50, pageToken=page).execute()
        for ref in resp.get("messages", []):
            if message_known(ref["id"]):
                continue
            head = svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
            meta = _meta(head["id"], head["payload"].get("headers", []))
            ok, reason = filters.decide(rules, meta["sender_email"], meta["subject"])
            if not ok:
                on_ignored(meta, reason)
                continue

            if with_att is None:
                with_att = _ids_with_attachments()
            if ref["id"] not in with_att:
                # no attachment, so file names cannot qualify it: decide now, without opening the email
                ok, reason = filters.decide(rules, meta["sender_email"], meta["subject"], has_attachment=False)
                if not ok:
                    on_ignored(meta, reason)
                    continue

            msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            att_parts, plain, html = _split(msg)
            ok, reason = filters.decide(rules, meta["sender_email"], meta["subject"],
                                        has_attachment=bool(att_parts), filenames=[f for f, _ in att_parts])
            if not ok:
                on_ignored(meta, reason)
                continue

            atts = _download(svc, msg["id"], meta["source_key"], att_parts, already_seen)
            body_text = plain or (parse_html(html.encode()) if html else "")
            if atts:
                yield meta, atts, body_text
            elif not att_parts and not already_seen(f"{meta['source_key']}:email-body.txt"):
                yield meta, atts, body_text  # document is written in the email body itself
        page = resp.get("nextPageToken")
        if not page:
            break


def fetch_one(message_id: str, already_seen: Callable[[str], bool]) -> tuple[dict, list[tuple[str, bytes]], str]:
    """Read ONE message regardless of the filters (used by 'Process anyway')."""
    svc = _service()
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    meta = _meta(msg["id"], msg["payload"].get("headers", []))
    att_parts, plain, html = _split(msg)
    atts = _download(svc, msg["id"], meta["source_key"], att_parts, already_seen)
    return meta, atts, plain or (parse_html(html.encode()) if html else "")
