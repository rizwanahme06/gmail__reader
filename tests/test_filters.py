import base64

import pytest
from fastapi.testclient import TestClient

from app import config, db, filters, gmail_client, pipeline
from app.filters import Rules, decide, gmail_query


# ---------------- rule engine ----------------

def R(**kw):
    return Rules(**kw)


def final(r, sender, subject, files=()):
    """The decision once the email has been fully looked at (stage 2)."""
    return decide(r, sender, subject, has_attachment=bool(files), filenames=list(files))


def test_defaults_accept_quotation_words_and_reject_chitchat():
    r = R()
    assert decide(r, "a@b.com", "Quotation QT-1 for brackets")[0]
    assert decide(r, "a@b.com", "PO-4521 attached")[0]
    assert not final(r, "a@b.com", "Lunch tomorrow?")[0]
    assert not final(r, "a@b.com", "Weekly report")[0]             # 'po' must not match inside 'report'
    assert not final(r, "a@b.com", "A popular offer")[0]


def test_allowed_sender_passes_any_subject_and_domain_covers_subdomains():
    r = R(allowed_senders=["acme.com", "orders@x.in"])
    assert decide(r, "buyer@acme.com", "hello")[0]
    assert decide(r, "buyer@mail.acme.com", "hello")[0]
    assert decide(r, "ORDERS@X.IN", "hello")[0]
    assert not final(r, "someone@notacme.com", "hello")[0]         # 'notacme.com' is not 'acme.com'


def test_blocked_beats_allowed_and_keywords():
    r = R(allowed_senders=["acme.com"], blocked_senders=["spam@acme.com", "noreply@"])
    assert not decide(r, "spam@acme.com", "Purchase order")[0]
    assert not decide(r, "noreply@shop.com", "Quotation")[0]
    assert decide(r, "ok@acme.com", "hi")[0]


def test_blocked_subject_word_beats_everything():
    r = R(allowed_senders=["acme.com"])
    ok, why = decide(r, "a@acme.com", "Webinar: purchase order tips")
    assert not ok and "webinar" in why.lower()


def test_only_allowed_senders_is_strict():
    r = R(allowed_senders=["acme.com"], only_allowed_senders=True)
    assert not decide(r, "x@other.com", "Quotation attached")[0]
    assert decide(r, "x@acme.com", "anything")[0]
    # strict mode with an empty list must not silently block everything
    assert decide(R(only_allowed_senders=True), "x@other.com", "Quotation")[0]


def test_attachment_required_applies_only_at_final_stage():
    r = R(require_attachment=True, allowed_senders=["acme.com"])
    assert decide(r, "a@acme.com", "x")[0]                                   # headers-only look: undecided -> pass
    assert not decide(r, "a@acme.com", "x", has_attachment=False)[0]
    assert decide(r, "a@acme.com", "x", has_attachment=True, filenames=["a.pdf"])[0]


def test_attachment_name_can_qualify_an_email():
    r = R()
    assert decide(r, "a@b.com", "Documents")[0]                              # stage 1: not sure yet
    assert decide(r, "a@b.com", "Documents", True, ["PO_4521.pdf"])[0]
    assert not decide(r, "a@b.com", "Documents", True, ["flyer.pdf"])[0]


def test_rules_accept_textarea_style_input():
    r = Rules.model_validate({"allowed_senders": "A@Acme.com,\n acme.com ;  b.com", "keywords": ""})
    assert r.allowed_senders == ["a@acme.com", "acme.com", "b.com"] and r.keywords == []


def test_gmail_query_narrowing():
    r = R(blocked_senders=["spam.com", "noreply@"], gmail_label="QuotePO",
          allowed_senders=["acme.com"], only_allowed_senders=True)
    q = gmail_query(r, "newer_than:7d")
    assert 'newer_than:7d' in q and 'label:"QuotePO"' in q and "from:(acme.com)" in q and "-from:(spam.com)" in q
    assert "noreply@" not in q                                               # prefix rules are checked locally
    assert gmail_query(R(), "newer_than:7d") == "newer_than:7d"
    assert "from:(" not in gmail_query(R(allowed_senders=["acme.com"]), "")  # non-strict must not narrow


def test_injection_in_rules_cannot_widen_the_gmail_query():
    q = gmail_query(R(blocked_senders=["x.com) OR (in:anywhere"]), "")
    assert "in:anywhere" not in q


# ---------------- fake Gmail account ----------------

def _b64(b: bytes | str) -> str:
    b = b.encode() if isinstance(b, str) else b
    return base64.urlsafe_b64encode(b).decode()


class _Req:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class FakeGmail:
    def __init__(self):
        self.msgs, self.atts, self.calls, self.queries = {}, {}, [], []

    def add(self, mid, frm, subject, files=(), body="hello"):
        headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject},
                   {"name": "Date", "value": "Mon, 12 Oct 2026 10:00:00 +0530"}]
        parts = [{"mimeType": "text/plain", "body": {"data": _b64(body)}}]
        for i, (fn, data) in enumerate(files):
            self.atts[f"{mid}-{i}"] = data
            parts.append({"filename": fn, "mimeType": "application/pdf", "body": {"attachmentId": f"{mid}-{i}"}})
        self.msgs[mid] = {"id": mid, "payload": {"headers": headers, "parts": parts}}

    # --- the bits of the Google client the app uses ---
    def users(self): return self
    def messages(self): return _Msgs(self)


class _Msgs:
    def __init__(self, f): self.f = f
    def list(self, userId, q, maxResults, pageToken=None):
        self.f.queries.append(q)
        ids = [i for i, m in self.f.msgs.items()
               if "has:attachment" not in q or any(p.get("filename") for p in m["payload"]["parts"])]
        return _Req({"messages": [{"id": i} for i in ids]})
    def get(self, userId, id, format, metadataHeaders=None):
        self.f.calls.append((format, id))
        m = self.f.msgs[id]
        return _Req({"id": id, "payload": {"headers": m["payload"]["headers"]}} if format == "metadata" else m)
    def attachments(self): return _Atts(self.f)


class _Atts:
    def __init__(self, f): self.f = f
    def get(self, userId, messageId, id):
        self.f.calls.append(("attachment", messageId))
        return _Req({"data": _b64(self.f.atts[id])})


@pytest.fixture()
def gmail(samples, fresh_db, monkeypatch):
    g = FakeGmail()
    po = (samples / "PO_4521.pdf").read_bytes()
    qt = (samples / "quotation_QT-2026-0042.pdf").read_bytes()
    g.add("m1", "Promo <noreply@promo.example.com>", "Quote of the day!")
    g.add("m2", "Sharma <orders@sharma.in>", "Order attached", [("scan001.pdf", po)])
    g.add("m3", "Friend <friend@gmail.com>", "Lunch tomorrow?")
    g.add("m4", "Verma <sales@verma.com>", "Documents", [("quotation_QT-2026-0042.pdf", qt)])
    g.add("m5", "Deals <deals@shop.com>", "Big sale", [("flyer.pdf", qt)])
    g.add("m6", "Org <hi@events.org>", "Webinar: purchase order automation")
    monkeypatch.setattr(gmail_client, "_service", lambda: g)
    filters.save(Rules(allowed_senders=["sharma.in"], blocked_senders=["noreply@"]))
    return g


def fulls(g): return sorted(i for f, i in g.calls if f == "full")
def downloads(g): return sorted(i for f, i in g.calls if f == "attachment")


def test_unwanted_mail_is_never_downloaded_or_read(gmail):
    res = pipeline.sync_gmail()
    assert res["added"] == 2 and res["ignored"] == 4 and res["error"] is None
    # headers (+ the has:attachment search) were enough to drop m1, m3, m6 WITHOUT opening them;
    # only m5 needed a full look, to read its attachment name
    assert fulls(gmail) == ["m2", "m4", "m5"]
    assert downloads(gmail) == ["m2", "m4"]                       # the flyer was never downloaded
    ign = {r["message_id"]: r["reason"] for r in db.list_ignored()}
    assert set(ign) == {"m1", "m3", "m5", "m6"}
    assert "blocked list" in ign["m1"] and "no quotation/PO word" in ign["m3"] and "webinar" in ign["m6"].lower()
    with db.conn() as c:
        assert {r["message_id"] for r in c.execute("SELECT message_id FROM documents")} == {"m2", "m4"}
        note = c.execute("SELECT body FROM notifications WHERE kind='sync'").fetchone()["body"]
    assert "4 skipped by your filters" in note


def test_second_sync_does_not_refetch_anything(gmail):
    pipeline.sync_gmail()
    gmail.calls.clear()
    res = pipeline.sync_gmail()
    assert gmail.calls == [] and res["added"] == 0 and res["ignored"] == 0


def test_gmail_search_is_narrowed_by_rules(gmail):
    pipeline.sync_gmail()
    assert "-from:(" not in gmail.queries[0]                       # 'noreply@' is a prefix rule -> checked locally
    filters.save(Rules(blocked_senders=["promo.example.com"]))
    pipeline.sync_gmail()
    assert "-from:(promo.example.com)" in gmail.queries[-1]


def test_changing_rules_rechecks_previously_ignored_mail(gmail, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_resync_in_background", lambda: None)
    pipeline.sync_gmail()
    with TestClient(main.app) as c:
        r = Rules(allowed_senders=["sharma.in", "shop.com"], blocked_senders=["noreply@"])
        assert c.put("/api/filters", json=r.model_dump()).status_code == 200
        assert c.get("/api/ignored").json() == []                  # list cleared so it gets re-evaluated
        res = pipeline.sync_gmail()
        assert res["added"] == 1                                    # the 'Big sale' mail is now wanted
        assert {x["message_id"] for x in c.get("/api/ignored").json()} == {"m1", "m3", "m6"}


def test_process_anyway_and_always_allow(gmail, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_resync_in_background", lambda: None)
    config.GMAIL_TOKEN_FILE.write_text("{}")
    try:
        pipeline.sync_gmail()
        with TestClient(main.app) as c:
            r = c.post("/api/ignored/m5/process?allow_sender=true")
            assert r.status_code == 200 and r.json()["added"] == 1
            assert "m5" not in {x["message_id"] for x in c.get("/api/ignored").json()}
            assert "deals@shop.com" in c.get("/api/filters").json()["allowed_senders"]
            assert c.post("/api/ignored/nope/process").status_code == 404
    finally:
        config.GMAIL_TOKEN_FILE.unlink(missing_ok=True)


def test_filter_api_roundtrip_and_tester(fresh_db):
    from app import main
    with TestClient(main.app) as c:
        assert c.get("/api/filters").json()["require_attachment"] is False
        body = {"rules": {"allowed_senders": ["acme.com"]}, "sender": "x@acme.com", "subject": "hi"}
        assert c.post("/api/filters/test", json=body).json()["process"] is True
        body["sender"] = "x@evil.com"
        t = c.post("/api/filters/test", json=body).json()
        assert t["process"] is False and t["reason"]
        assert c.put("/api/filters", json={"allowed_senders": "a.com\nb.com"}).json()["allowed_senders"] == ["a.com", "b.com"]
        assert c.get("/api/stats").json()["ignored"] == 0
