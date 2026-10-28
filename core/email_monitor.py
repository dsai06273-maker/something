"""Employer email monitoring: poll an IMAP mailbox, classify replies, update statuses.

Design notes
------------
* Read-only. The folder is opened with ``readonly=True`` and messages are fetched
  with ``BODY.PEEK`` so nothing is ever marked read, moved or deleted.
* Local-first. Mail is fetched and classified on this machine. Only a small
  record is stored for relevant messages (sender, subject, 300-char snippet).
  Irrelevant mail is remembered by Message-ID only, with no content.
* Conservative. Status changes require a confident classification AND an
  unambiguous match to a single application; everything else is surfaced as a
  suggestion the user can accept.

Auth: password / app-password IMAP over SSL. OAuth (needed for some Microsoft
accounts) is not implemented.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from typing import Callable, Iterator

from core.email_classifier import (
    ACTIONABLE_LABELS,
    OTHER,
    Classification,
    classify_email,
    match_application,
    next_status,
)

logger = logging.getLogger(__name__)

PROVIDER_HOSTS: dict[str, str] = {
    "gmail": "imap.gmail.com",
    "outlook": "outlook.office365.com",
    "yahoo": "imap.mail.yahoo.com",
    "icloud": "imap.mail.me.com",
}

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MAX_FETCH_BYTES = 65536  # first 64 KB of each message is plenty for classification
_MAX_MESSAGES_PER_CHECK = 300
_MIN_MATCH_SCORE_FOR_AUTO = 2
_SNIPPET_LEN = 300


class EmailMonitorError(Exception):
    """Raised for configuration/connection problems with a user-presentable message."""


@dataclass
class EmailMessage:
    message_id: str
    sender_name: str
    sender_addr: str
    subject: str
    received_at: str  # ISO-8601 or ""
    body: str


@dataclass
class CheckResult:
    scanned: int = 0
    new: int = 0
    matched: int = 0
    updated: int = 0
    suggested: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned, "new": self.new, "matched": self.matched,
            "updated": self.updated, "suggested": self.suggested, "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def parse_message(raw: bytes) -> EmailMessage:
    """Parse raw RFC 822 bytes (possibly truncated) into an EmailMessage."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(_decode(msg.get("From", "")))
    subject = _decode(msg.get("Subject", ""))

    received = ""
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            received = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass

    body = ""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            body = part.get_content()
        else:
            part = msg.get_body(preferencelist=("html",))
            if part is not None:
                body = _html_to_text(part.get_content())
    except Exception:  # truncated / malformed MIME
        payload = msg.get_payload(decode=True) if not msg.is_multipart() else None
        if payload:
            body = payload.decode("utf-8", errors="replace")

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        basis = f"{addr}|{subject}|{received}"
        message_id = "<synthetic-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32] + ">"
    return EmailMessage(message_id, name, addr, subject, received, body or "")


def _imap_date(d: date) -> str:
    # imaplib wants DD-Mon-YYYY with English month names, independent of locale.
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


# ---------------------------------------------------------------------------
# IMAP client (thin wrapper so tests can inject a fake)
# ---------------------------------------------------------------------------

class ImapSession:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str = "INBOX",
        timeout: int = 30,
        factory: Callable[..., "imaplib.IMAP4"] | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.folder, self.timeout = folder, timeout
        self._factory = factory or imaplib.IMAP4_SSL
        self._conn: imaplib.IMAP4 | None = None
        self.scanned = 0

    def __enter__(self) -> "ImapSession":
        try:
            self._conn = self._factory(self.host, self.port, timeout=self.timeout)
            self._conn.login(self.username, self.password)
            typ, data = self._conn.select(self._quote_folder(self.folder), readonly=True)
            if typ != "OK":
                raise EmailMonitorError(f"Could not open folder '{self.folder}'.")
            self._count = int(data[0]) if data and data[0] else 0
        except imaplib.IMAP4.error as exc:
            self._close()
            raise EmailMonitorError(f"IMAP login failed: {_clean_err(exc)}") from exc
        except (OSError, TimeoutError) as exc:
            self._close()
            raise EmailMonitorError(f"Could not connect to {self.host}:{self.port}: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        self._close()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    @staticmethod
    def _quote_folder(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @property
    def message_count(self) -> int:
        return getattr(self, "_count", 0)

    def _ids_since(self, since: date) -> list[str]:
        assert self._conn is not None
        typ, data = self._conn.search(None, "SINCE", _imap_date(since))
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].decode().split()

    def _fetch_message_ids(self, ids: list[str]) -> dict[str, str]:
        """Map IMAP sequence number -> Message-ID header, in batches."""
        assert self._conn is not None
        out: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            typ, data = self._conn.fetch(",".join(chunk), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if typ != "OK" or not data:
                continue
            for item in data:
                if isinstance(item, tuple) and len(item) >= 2:
                    seq = item[0].split(b" ", 1)[0].decode()
                    m = re.search(rb"(?i)message-id:\s*(<[^>\r\n]+>)", item[1])
                    if m:
                        out[seq] = m.group(1).decode("utf-8", "replace")
        return out

    def _fetch_full(self, seq: str) -> bytes | None:
        assert self._conn is not None
        typ, data = self._conn.fetch(seq, f"(BODY.PEEK[]<0.{_MAX_FETCH_BYTES}>)")
        if typ != "OK" or not data:
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return None

    def iter_new_messages(
        self,
        since: date,
        is_known: Callable[[list[str]], set[str]],
        limit: int = _MAX_MESSAGES_PER_CHECK,
    ) -> Iterator[EmailMessage]:
        """Yield messages not already processed; sets ``self.scanned`` to the number examined.

        `is_known` takes a list of Message-IDs and returns those already stored.
        Messages without a Message-ID header are fetched and deduped on a synthetic id.
        """
        ids = self._ids_since(since)[-limit:]
        self.scanned = len(ids)
        header_ids = self._fetch_message_ids(ids)
        known = is_known(list(header_ids.values()))
        for seq in ids:
            mid = header_ids.get(seq)
            if mid is not None and mid in known:
                continue
            raw = self._fetch_full(seq)
            if not raw:
                continue
            yield parse_message(raw)


def _clean_err(exc: Exception) -> str:
    text = " ".join(
        a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a) for a in exc.args
    )
    return text[:200] or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

def resolve_host(provider: str, custom_host: str) -> str:
    if provider == "custom":
        return custom_host.strip()
    return PROVIDER_HOSTS.get(provider, custom_host.strip())


class EmailMonitor:
    """Runs one check: fetch -> classify -> match -> (maybe) update status."""

    def __init__(
        self,
        db,
        get_config: Callable[[], object | None],
        get_password: Callable[[], str],
        session_factory: Callable[..., ImapSession] = ImapSession,
        on_status_update: Callable[[dict], None] | None = None,
    ) -> None:
        self._db = db
        self._get_config = get_config
        self._get_password = get_password
        self._session_factory = session_factory
        self._on_status_update = on_status_update

    def _email_cfg(self):
        cfg = self._get_config()
        email_cfg = getattr(cfg, "email", None)
        if email_cfg is None:
            raise EmailMonitorError("Email monitoring is not configured.")
        return email_cfg

    def open_session(self, cfg=None, password: str | None = None) -> ImapSession:
        cfg = cfg or self._email_cfg()
        host = resolve_host(cfg.provider, cfg.imap_host)
        pw = password if password is not None else self._get_password()
        if not host or not cfg.username or not pw:
            raise EmailMonitorError("Email host, username and password are required.")
        return self._session_factory(host, cfg.imap_port, cfg.username, pw, cfg.folder)

    def check_once(self) -> CheckResult:
        cfg = self._email_cfg()
        result = CheckResult()
        since = (datetime.now(timezone.utc) - timedelta(days=cfg.lookback_days)).date()
        open_apps = self._db.get_open_applications()

        with self.open_session(cfg) as session:
            for msg in session.iter_new_messages(since, self._db.get_known_message_ids):
                result.new += 1
                try:
                    self._process(msg, cfg, open_apps, result)
                except Exception as exc:  # one bad message must not abort the run
                    logger.exception("Failed processing email")
                    result.errors.append(str(exc)[:200])
            result.scanned = session.scanned
        return result

    def _process(self, msg: EmailMessage, cfg, open_apps: list[dict], result: CheckResult) -> None:
        if self._db.has_email_message(msg.message_id):
            return  # e.g. header-less message already handled on an earlier poll
        cls: Classification = classify_email(msg.subject, msg.body)
        own = (getattr(getattr(self._get_config(), "profile", None), "email", "") or "").lower()
        if cls.label == OTHER or (own and msg.sender_addr.lower() == own):
            # Remember the id only — no content is kept for irrelevant mail.
            self._db.save_email_message(msg.message_id, OTHER, 0.0, "ignored")
            return

        match = match_application(open_apps, msg.sender_name, msg.sender_addr, msg.subject, msg.body)
        if match is None:
            self._db.save_email_message(msg.message_id, OTHER, 0.0, "ignored")
            return
        result.matched += 1

        app = next((a for a in open_apps if a["id"] == match.application_id), None)
        current = app["status"] if app else ""
        proposed = next_status(current, cls.label) if cls.label in ACTIONABLE_LABELS else None

        action = "noted"  # acknowledgement / assessment / no applicable transition
        if proposed:
            confident = (
                cls.confidence >= cfg.min_confidence
                and not match.ambiguous
                and match.score >= _MIN_MATCH_SCORE_FOR_AUTO
            )
            if cfg.auto_update_status and confident:
                reason = f"Employer email ({cls.label}): {msg.subject[:120]}"
                if self._db.apply_status_change(match.application_id, proposed, "email", reason):
                    action = "status_updated"
                    result.updated += 1
                    if app:
                        app["status"] = proposed  # keep in-memory view current for later messages
                    self._notify(match.application_id, proposed, cls.label, msg.subject)
            if action != "status_updated":
                action = "suggested"
                result.suggested += 1

        self._db.save_email_message(
            msg.message_id, cls.label, cls.confidence, action,
            application_id=match.application_id,
            sender=f"{msg.sender_name} <{msg.sender_addr}>".strip(),
            subject=msg.subject[:300],
            received_at=msg.received_at,
            proposed_status=proposed,
            snippet=re.sub(r"\s+", " ", msg.body).strip()[:_SNIPPET_LEN],
        )

    def _notify(self, app_id: int, new_status: str, label: str, subject: str) -> None:
        application = self._db.get_application(app_id)
        if application is not None:
            self._db.save_feed_event(
                "email_status", application.job_title, application.company, application.platform,
                f"Status → {new_status} (from employer email)",
            )
        if self._on_status_update:
            try:
                self._on_status_update({
                    "application_id": app_id, "status": new_status,
                    "classification": label, "subject": subject,
                })
            except Exception:
                logger.debug("status-update callback failed", exc_info=True)

    def accept_suggestion(self, email_id: int) -> bool:
        """Apply a previously 'suggested' status change after user confirmation."""
        row = self._db.get_email_message(email_id)
        if not row or row["action"] != "suggested" or not row["application_id"] or not row["proposed_status"]:
            return False
        application = self._db.get_application(row["application_id"])
        if application is None:
            return False
        # Re-validate against the current status so a stale suggestion can't downgrade anything.
        allowed = next_status(application.status, row["classification"])
        if allowed != row["proposed_status"]:
            self._db.set_email_action(email_id, "dismissed")
            return False
        reason = f"Employer email ({row['classification']}), confirmed by user: {(row['subject'] or '')[:120]}"
        ok = self._db.apply_status_change(row["application_id"], allowed, "email", reason)
        self._db.set_email_action(email_id, "status_updated" if ok else "dismissed")
        if ok:
            self._notify(row["application_id"], allowed, row["classification"], row["subject"] or "")
        return ok


# ---------------------------------------------------------------------------
# Background service
# ---------------------------------------------------------------------------

class EmailMonitorService:
    """Polls on an interval in a daemon thread. start()/stop() are idempotent."""

    def __init__(self, monitor: EmailMonitor, get_interval_minutes: Callable[[], int]) -> None:
        self._monitor = monitor
        self._get_interval = get_interval_minutes
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # serialises checks (loop vs. "check now")
        self.last_check_at: str | None = None
        self.last_error: str | None = None
        self.last_result: dict | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="email-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None

    def check_now(self) -> dict:
        """Run a check synchronously (used by the API). Raises EmailMonitorError."""
        with self._lock:
            try:
                result = self._monitor.check_once().as_dict()
                self.last_error = None
                self.last_result = result
                return result
            except EmailMonitorError as exc:
                self.last_error = str(exc)
                raise
            finally:
                self.last_check_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except EmailMonitorError as exc:
                logger.warning("Email check failed: %s", exc)
            except Exception:
                logger.exception("Unexpected email monitor error")
                self.last_error = "Unexpected error — see log."
            interval = max(5, int(self._get_interval())) * 60
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def status(self) -> dict:
        return {
            "running": self.running,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }
