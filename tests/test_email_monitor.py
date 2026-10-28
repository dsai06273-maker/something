"""Tests for core.email_monitor using a fake IMAP server and a real SQLite Database."""

from __future__ import annotations

import imaplib
from email.message import EmailMessage as MimeMessage
from types import SimpleNamespace

import pytest

from config.settings import AppConfig, EmailConfig
from core.email_monitor import (
    EmailMonitor,
    EmailMonitorError,
    EmailMonitorService,
    ImapSession,
    parse_message,
    resolve_host,
)
from db.database import Database

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def make_raw(subject: str, body: str, sender: str = "Acme Recruiting <jobs@acme.com>",
             message_id: str | None = "<m1@x>", html: bool = False) -> bytes:
    msg = MimeMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 26 Oct 2026 10:00:00 +0000"
    if message_id:
        msg["Message-ID"] = message_id
    if html:
        msg.set_content(body, subtype="html")  # HTML-only message
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapSession. Tracks that nothing is mutated."""

    instances: list["FakeImap"] = []

    def __init__(self, messages: list[bytes], fail_login: bool = False):
        self.messages = messages
        self.fail_login = fail_login
        self.selected_readonly: bool | None = None
        self.fetch_specs: list[str] = []
        self.logged_out = False

    # factory protocol: IMAP4_SSL(host, port, timeout=...)
    def __call__(self, host, port, timeout=30):
        return self

    def login(self, user, pw):
        if self.fail_login:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.selected_readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, *criteria):
        ids = " ".join(str(i + 1) for i in range(len(self.messages))).encode()
        return "OK", [ids]

    def fetch(self, seqset, spec):
        self.fetch_specs.append(spec)
        out = []
        for seq in seqset.split(","):
            raw = self.messages[int(seq) - 1]
            if "HEADER.FIELDS" in spec:
                import email
                mid = email.message_from_bytes(raw).get("Message-ID")
                payload = (f"Message-ID: {mid}\r\n\r\n" if mid else "\r\n").encode()
                out.append((f"{seq} (BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(payload)}}}".encode(), payload))
            else:
                out.append((f"{seq} (BODY[]<0> {{{len(raw)}}}".encode(), raw))
            out.append(b")")
        return "OK", out

    def logout(self):
        self.logged_out = True
        return "BYE", []


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def add_app(db, company="Acme Inc.", title="Software Engineer", status="applied", ext="e1"):
    app_id = db.save_application(
        external_id=ext, platform="linkedin", job_title=title, company=company,
        location=None, salary=None, apply_url="https://x", match_score=90,
        resume_path=None, cover_letter_path=None, cover_letter_text=None,
        status=status, error_message=None,
    )
    return app_id


def make_monitor(db, fake: FakeImap, **cfg_overrides):
    cfg = AppConfig(
        profile={"first_name": "J", "last_name": "D", "email": "me@example.com", "phone": "1",
                 "city": "NYC", "state": "NY", "bio": "b"},
        search_criteria={"job_titles": ["x"], "locations": ["y"]},
        email=EmailConfig(enabled=True, username="me@example.com", **cfg_overrides),
    )
    events: list[dict] = []
    monitor = EmailMonitor(
        db, get_config=lambda: cfg, get_password=lambda: "secret",
        session_factory=lambda host, port, user, pw, folder: ImapSession(host, port, user, pw, folder, factory=fake),
        on_status_update=events.append,
    )
    return monitor, events


REJECTION = ("Update on your application to Acme",
             "Unfortunately, we have decided to move forward with other candidates. Thank you, Acme Inc.")

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParse:
    def test_plain_text(self):
        m = parse_message(make_raw("Hello", "Body text"))
        assert (m.sender_name, m.sender_addr, m.subject) == ("Acme Recruiting", "jobs@acme.com", "Hello")
        assert "Body text" in m.body and m.message_id == "<m1@x>"
        assert m.received_at.startswith("2026-10-26T10:00:00")

    def test_html_only_body_is_stripped(self):
        raw = make_raw("H", "<html><style>p{}</style><p>We will not be moving forward</p></html>", html=True)
        m = parse_message(raw)
        assert "not be moving forward" in m.body and "<p>" not in m.body

    def test_missing_message_id_gets_stable_synthetic_id(self):
        a = parse_message(make_raw("Hi", "x", message_id=None))
        b = parse_message(make_raw("Hi", "x", message_id=None))
        assert a.message_id == b.message_id and a.message_id.startswith("<synthetic-")

    def test_encoded_subject(self):
        raw = make_raw("=?utf-8?q?Entrevista_con_Acme_=C3=A1?=", "x")
        assert parse_message(raw).subject == "Entrevista con Acme á"

    def test_resolve_host(self):
        assert resolve_host("gmail", "") == "imap.gmail.com"
        assert resolve_host("custom", " mail.example.org ") == "mail.example.org"


# ---------------------------------------------------------------------------
# End-to-end check_once
# ---------------------------------------------------------------------------


class TestCheckOnce:
    def test_rejection_auto_updates_status_notes_history_and_notifies(self, db):
        app_id = add_app(db)
        db.update_status(app_id, "applied", notes="my own note")
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, events = make_monitor(db, fake)

        result = monitor.check_once()

        assert (result.new, result.matched, result.updated) == (1, 1, 1)
        a = db.get_application(app_id)
        assert a.status == "rejected"
        assert "my own note" in a.notes and "[email]" in a.notes  # appended, not overwritten
        hist = db.get_status_history(app_id)
        assert hist[0]["old_status"] == "applied" and hist[0]["new_status"] == "rejected" and hist[0]["source"] == "email"
        assert events and events[0]["status"] == "rejected"
        assert any(e.event_type == "email_status" for e in db.get_feed_events())

    def test_mailbox_is_never_mutated(self, db):
        add_app(db)
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert fake.selected_readonly is True
        assert all("PEEK" in s for s in fake.fetch_specs)
        assert fake.logged_out

    def test_second_run_skips_known_messages(self, db):
        add_app(db)
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        fake.fetch_specs.clear()
        r2 = monitor.check_once()
        assert r2.new == 0 and r2.scanned == 1
        assert not any("BODY.PEEK[]<" in s for s in fake.fetch_specs)  # no full-body refetch

    def test_irrelevant_mail_stores_no_content(self, db):
        add_app(db)
        fake = FakeImap([make_raw("Weekly newsletter", "Ten interview tips for you", sender="News <n@news.com>")])
        monitor, _ = make_monitor(db, fake)
        r = monitor.check_once()
        assert r.new == 1 and r.matched == 0
        assert db.get_email_messages() == []  # 'ignored' rows are not exposed
        with db._connect() as conn:
            row = conn.execute("SELECT * FROM email_messages").fetchone()
        assert row["action"] == "ignored" and row["subject"] is None and row["snippet"] is None and row["sender"] is None

    def test_unmatched_company_changes_nothing(self, db):
        app_id = add_app(db, company="Globex")
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert db.get_application(app_id).status == "applied"

    def test_ambiguous_match_is_suggested_not_applied(self, db):
        a1 = add_app(db, title="Software Engineer", ext="e1")
        a2 = add_app(db, title="Data Analyst", ext="e2")
        fake = FakeImap([make_raw("Update from Acme", "We will not be moving forward with your application.",
                                  sender="Acme <no-reply@greenhouse.io>")])
        monitor, _ = make_monitor(db, fake)
        r = monitor.check_once()
        assert r.updated == 0 and r.suggested == 1
        assert db.get_application(a1).status == db.get_application(a2).status == "applied"

    def test_low_confidence_is_suggested(self, db):
        app_id = add_app(db)
        fake = FakeImap([make_raw("Acme", "Acme: we regret to inform you of an update.")])
        monitor, _ = make_monitor(db, fake)
        r = monitor.check_once()
        assert r.updated == 0
        assert db.get_application(app_id).status == "applied"

    def test_auto_update_off_then_user_accepts_suggestion(self, db):
        app_id = add_app(db)
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, events = make_monitor(db, fake, auto_update_status=False)
        r = monitor.check_once()
        assert r.suggested == 1 and db.get_application(app_id).status == "applied"
        msg = db.get_email_messages(action="suggested")[0]
        assert msg["proposed_status"] == "rejected"

        assert monitor.accept_suggestion(msg["id"]) is True
        assert db.get_application(app_id).status == "rejected"
        assert events[-1]["status"] == "rejected"
        assert db.get_email_message(msg["id"])["action"] == "status_updated"
        assert monitor.accept_suggestion(msg["id"]) is False  # not re-appliable

    def test_stale_suggestion_cannot_downgrade(self, db):
        app_id = add_app(db)
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, _ = make_monitor(db, fake, auto_update_status=False)
        monitor.check_once()
        db.update_status(app_id, "offer")  # user got an offer in the meantime
        msg = db.get_email_messages(action="suggested")[0]
        assert monitor.accept_suggestion(msg["id"]) is False
        assert db.get_application(app_id).status == "offer"
        assert db.get_email_message(msg["id"])["action"] == "dismissed"

    def test_offer_is_not_downgraded_by_later_rejection_email(self, db):
        app_id = add_app(db, status="offer")
        fake = FakeImap([make_raw(*REJECTION)])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert db.get_application(app_id).status == "offer"

    def test_messages_processed_oldest_first_so_final_state_is_latest(self, db):
        app_id = add_app(db)
        fake = FakeImap([
            make_raw("Interview at Acme", "We would like to speak with you. Please schedule via calendly.com/acme/x",
                     message_id="<a@x>"),
            make_raw(*REJECTION, message_id="<b@x>"),
        ])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert db.get_application(app_id).status == "rejected"
        assert [h["new_status"] for h in reversed(db.get_status_history(app_id))] == ["interview", "rejected"]

    def test_acknowledgement_is_noted_without_status_change(self, db):
        app_id = add_app(db)
        fake = FakeImap([make_raw("Thanks for applying to Acme", "We have received your application at Acme.")])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert db.get_application(app_id).status == "applied"
        assert db.get_email_messages()[0]["action"] == "noted"

    def test_own_address_ignored(self, db):
        app_id = add_app(db)
        fake = FakeImap([make_raw(*REJECTION, sender="Me <me@example.com>")])
        monitor, _ = make_monitor(db, fake)
        monitor.check_once()
        assert db.get_application(app_id).status == "applied"

    def test_bad_credentials_raise_clean_error_without_password(self, db):
        fake = FakeImap([], fail_login=True)
        monitor, _ = make_monitor(db, fake)
        with pytest.raises(EmailMonitorError) as exc:
            monitor.check_once()
        assert "login failed" in str(exc.value).lower() and "secret" not in str(exc.value)

    def test_missing_credentials_raise(self, db):
        monitor, _ = make_monitor(db, FakeImap([]))
        monitor._get_password = lambda: ""
        with pytest.raises(EmailMonitorError):
            monitor.check_once()


class TestService:
    def test_check_now_records_status_and_errors(self, db):
        add_app(db)
        monitor, _ = make_monitor(db, FakeImap([make_raw(*REJECTION)]))
        svc = EmailMonitorService(monitor, lambda: 15)
        res = svc.check_now()
        assert res["updated"] == 1 and svc.last_error is None and svc.last_check_at

        monitor._get_password = lambda: ""
        with pytest.raises(EmailMonitorError):
            svc.check_now()
        assert svc.last_error

    def test_start_stop_idempotent(self, db):
        monitor, _ = make_monitor(db, FakeImap([]))
        svc = EmailMonitorService(monitor, lambda: 15)
        svc.start(); svc.start()
        assert svc.running
        svc.stop(); svc.stop()
        assert not svc.running
