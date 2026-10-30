"""Decide which emails are worth processing.

Emails that fail the rules are IGNORED before any attachment is downloaded and before any AI call,
so unwanted mail costs nothing. Ignored emails are logged (with the reason) so you can rescue one.

Order of decisions (first match wins):
  1. sender on the blocked list            -> ignore
  2. subject has a blocked word            -> ignore
  3. "attachments only" is on, none found  -> ignore
  4. sender on the allowed list            -> PROCESS (trusted, subject can be anything)
  5. "only allowed senders" is on          -> ignore
  6. subject / attachment name has one of the keywords (or no keywords set) -> PROCESS, else ignore
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from pydantic import BaseModel, field_validator

from . import db


def _clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\n,;]+", v)
    out: list[str] = []
    for x in v or []:
        x = str(x).strip().lower()
        if x and x not in out:
            out.append(x)
    return out


class Rules(BaseModel):
    allowed_senders: list[str] = []          # "buyer@acme.com", "acme.com" or "@acme.com"
    only_allowed_senders: bool = False       # strict: nobody else gets through, whatever the subject
    blocked_senders: list[str] = []          # same syntax; "noreply@" blocks every address starting with it
    keywords: list[str] = ["quotation", "quote", "purchase order", "po", "rfq", "proforma"]
    blocked_keywords: list[str] = ["newsletter", "unsubscribe", "webinar", "out of office", "automatic reply"]
    require_attachment: bool = False
    gmail_label: str = ""                    # optional: only look inside this Gmail label

    @field_validator("allowed_senders", "blocked_senders", "keywords", "blocked_keywords", mode="before")
    @classmethod
    def _lists(cls, v):
        return _clean_list(v)

    @field_validator("gmail_label", mode="before")
    @classmethod
    def _label(cls, v):
        return (v or "").strip().replace('"', "")

    def fingerprint(self) -> str:
        """Changes whenever the rules change, so previously ignored mails get re-checked."""
        return hashlib.sha1(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:12]


# ---------------- storage ----------------

def load() -> Rules:
    raw = db.get_setting("filters")
    try:
        return Rules.model_validate(json.loads(raw)) if raw else Rules()
    except Exception:
        return Rules()


def save(rules: Rules) -> Rules:
    db.set_setting("filters", rules.model_dump_json())
    return rules


# ---------------- matching ----------------

def sender_matches(entry: str, email: str) -> bool:
    if not email or not entry:
        return False
    if entry.endswith("@"):                       # "noreply@" -> any address starting with it
        return email.startswith(entry)
    bare = entry.lstrip("@")
    if "@" in bare:                               # full address
        return email == bare
    host = email.rsplit("@", 1)[-1]               # domain (subdomains count)
    return host == bare or host.endswith("." + bare)


def keyword_hit(words: Iterable[str], text: str) -> str | None:
    """Whole-word match, so 'po' finds 'PO-4521' / 'PO_4521.pdf' but not 'report' or 'popular'."""
    for w in words:
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}s?(?![a-z])", text or "", re.I):
            return w
    return None


def evaluate(rules: Rules, sender_email: str, subject: str, filenames: Iterable[str] = (),
             has_attachment: bool | None = None, final: bool = True) -> tuple[bool, str]:
    """Return (process?, human-readable reason).

    final=False is the cheap first look from headers only (sender + subject): it rejects only on
    rules that are already certain, and lets everything else through to the final look, which also
    knows the attachment names."""
    email = (sender_email or "").strip().lower()
    names = [n for n in filenames if n]
    has_att = bool(names) if has_attachment is None else has_attachment

    for e in rules.blocked_senders:
        if sender_matches(e, email):
            return False, f"Sender is on your blocked list ({e})"
    hit = keyword_hit(rules.blocked_keywords, subject)
    if hit:
        return False, f"Subject contains the blocked word \u201c{hit}\u201d"
    if final and rules.require_attachment and not has_att:
        return False, "No attachment (you chose attachments only)"
    trusted = next((e for e in rules.allowed_senders if sender_matches(e, email)), None)
    if trusted:
        return True, f"Trusted sender ({trusted})"
    if rules.only_allowed_senders and rules.allowed_senders:
        return False, "Sender is not on your allowed list"
    if rules.keywords:
        hit = keyword_hit(rules.keywords, " ".join([subject or "", *names]))
        if hit:
            return True, f"Subject or file name matches \u201c{hit}\u201d"
        if not final:
            return True, "Needs a look at the attachment names"
        return False, "Not from an allowed sender and no quotation/PO word in the subject or file name"
    return True, "No filter restricts this email"


def decide(rules: Rules, sender_email: str, subject: str, has_attachment: bool | None = None,
           filenames: Iterable[str] = ()) -> tuple[bool, str]:
    """Two-stage entry point used by the Gmail reader. Without has_attachment it is the cheap
    headers-only stage; with it, the final decision."""
    return evaluate(rules, sender_email, subject, filenames, has_attachment, final=has_attachment is not None)


# ---------------- Gmail search narrowing ----------------

_SAFE = re.compile(r"^[\w.+\-@]+$")


def gmail_query(rules: Rules, base: str = "") -> str:
    """Let Gmail itself drop the obvious mail, so it is not even listed. The exact rules are
    still re-checked locally; this only narrows the search."""
    parts = [base.strip()] if base.strip() else []
    if rules.gmail_label:
        parts.append(f'label:"{rules.gmail_label}"')
    allowed = [s for s in rules.allowed_senders if _SAFE.match(s) and not s.endswith("@")]
    if rules.only_allowed_senders and allowed:
        parts.append("from:(" + " OR ".join(allowed) + ")")
    blocked = [s for s in rules.blocked_senders if _SAFE.match(s) and not s.endswith("@")]
    if blocked:
        parts.append("-from:(" + " OR ".join(blocked) + ")")
    return " ".join(parts)
