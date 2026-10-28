"""Tests for /api/email/* routes and status-history endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db.database import Database

CONFIG = {
    "profile": {"first_name": "T", "last_name": "U", "email": "t@example.com", "phone": "555",
                "city": "NYC", "state": "NY", "bio": "bio"},
    "search_criteria": {"job_titles": ["Engineer"], "locations": ["NYC"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.get_data_dir", lambda: tmp_path)
    monkeypatch.setattr("config.settings._keyring_available", False)  # never touch the real OS keyring
    (tmp_path / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    test_db = Database(tmp_path / "test.db")
    monkeypatch.setattr("app_state.db", test_db)
    monkeypatch.setattr("app_state.email_service", None)

    from app import app
    app.config["TESTING"] = True
    return app.test_client(), tmp_path, test_db


def _put(c, **body):
    return c.put("/api/email/config", json=body)


class TestConfigRoutes:
    def test_defaults(self, client):
        c, _, _ = client
        d = c.get("/api/email/config").get_json()
        assert d["enabled"] is False and d["has_password"] is False and "gmail" in d["providers"]
        assert "password" not in d

    def test_save_and_password_is_never_returned_or_written_to_config_json(self, client):
        c, tmp, _ = client
        r = _put(c, provider="gmail", username="me@gmail.com", password="app-pass-123",
                 enabled=False, poll_interval_minutes=30)
        assert r.status_code == 200
        body = r.get_json()
        assert body["has_password"] is True and body["poll_interval_minutes"] == 30
        assert "app-pass-123" not in r.get_data(as_text=True)
        assert "app-pass-123" not in (tmp / "config.json").read_text()
        assert "app-pass-123" not in c.get("/api/email/config").get_data(as_text=True)

    def test_secret_file_fallback_has_private_permissions(self, client):
        c, tmp, _ = client
        _put(c, username="me@gmail.com", password="pw", enabled=False)
        f = tmp / ".email_secret"
        assert f.exists()
        import os, stat
        if os.name != "nt":
            assert stat.S_IMODE(f.stat().st_mode) == 0o600

    def test_blank_password_keeps_existing(self, client):
        c, _, _ = client
        _put(c, username="a@b.com", password="pw", enabled=False)
        r = _put(c, username="a@b.com", password="", enabled=False)
        assert r.get_json()["has_password"] is True

    def test_clear_password_disables(self, client):
        c, _, _ = client
        _put(c, username="a@b.com", password="pw", enabled=False)
        r = _put(c, clear_password=True)
        assert r.get_json()["has_password"] is False and r.get_json()["enabled"] is False

    @pytest.mark.parametrize("body", [
        {"provider": "aol"},
        {"imap_port": 0}, {"imap_port": 70000}, {"imap_port": "x"},
        {"poll_interval_minutes": 1}, {"lookback_days": 0}, {"lookback_days": 500},
        {"min_confidence": 0.1},
        {"imap_host": "evil.com/x"}, {"imap_host": "a b"},
        {"folder": "x" * 300},
    ])
    def test_validation_rejects_bad_input(self, client, body):
        c, _, _ = client
        assert _put(c, **body).status_code == 400

    def test_enabling_requires_username_and_password(self, client):
        c, _, _ = client
        assert _put(c, enabled=True).status_code == 400
        assert _put(c, enabled=True, username="a@b.com").status_code == 400
        assert _put(c, provider="custom", enabled=True, username="a@b.com", password="x").status_code == 400

    def test_requires_json_body(self, client):
        c, _, _ = client
        assert c.put("/api/email/config", data="nope").status_code == 400

    def test_enable_starts_and_disable_stops_service(self, client, monkeypatch):
        c, _, _ = client
        import routes.email_monitor as em
        started = {"n": 0, "stopped": 0}
        svc = em.get_service()
        monkeypatch.setattr(svc, "start", lambda: started.__setitem__("n", started["n"] + 1))
        monkeypatch.setattr(svc, "stop", lambda: started.__setitem__("stopped", started["stopped"] + 1))
        assert _put(c, enabled=True, username="a@b.com", password="pw").status_code == 200
        assert started["n"] == 1
        assert _put(c, enabled=False).status_code == 200
        assert started["stopped"] == 1


class TestTestAndCheck:
    def test_connection_error_is_reported_not_raised(self, client, monkeypatch):
        c, _, _ = client
        import core.email_monitor as mon

        def boom(*a, **k):
            raise OSError("no route")
        monkeypatch.setattr(mon.imaplib, "IMAP4_SSL", boom)
        r = c.post("/api/email/test", json={"username": "a@b.com", "password": "pw", "provider": "gmail"})
        assert r.status_code == 400 and r.get_json()["ok"] is False
        assert "pw" not in r.get_data(as_text=True).replace("password", "")

    def test_connection_ok(self, client, monkeypatch):
        c, _, _ = client
        import core.email_monitor as mon
        from tests.test_email_monitor import FakeImap
        monkeypatch.setattr(mon.imaplib, "IMAP4_SSL", FakeImap([b"x", b"y"]))
        r = c.post("/api/email/test", json={"username": "a@b.com", "password": "pw"})
        assert r.get_json() == {"ok": True, "message_count": 2}

    def test_test_endpoint_rejects_invalid_override(self, client):
        c, _, _ = client
        assert c.post("/api/email/test", json={"imap_port": 99999}).status_code == 400

    def test_check_requires_enabled(self, client):
        c, _, _ = client
        assert c.post("/api/email/check").status_code == 409


class TestMessagesAndHistory:
    def _seed(self, db):
        app_id = db.save_application("e1", "linkedin", "Engineer", "Acme", None, None, "u", 90,
                                     None, None, None, "applied", None)
        eid = db.save_email_message("<1@x>", "rejected", 0.9, "suggested", application_id=app_id,
                                    sender="A <a@acme.com>", subject="No", proposed_status="rejected",
                                    snippet="sorry")
        return app_id, eid

    def test_list_filters_and_hides_ignored(self, client):
        c, _, db = client
        app_id, eid = self._seed(db)
        db.save_email_message("<2@x>", "other", 0.0, "ignored")
        rows = c.get("/api/email/messages").get_json()
        assert [r["id"] for r in rows] == [eid]
        assert c.get(f"/api/email/messages?application_id={app_id}&action=suggested").get_json()[0]["id"] == eid
        assert c.get("/api/email/messages?action=noted").get_json() == []

    def test_apply_suggestion(self, client):
        c, _, db = client
        app_id, eid = self._seed(db)
        assert c.post(f"/api/email/messages/{eid}/apply").status_code == 200
        assert db.get_application(app_id).status == "rejected"
        assert c.post(f"/api/email/messages/{eid}/apply").status_code == 409  # already applied
        hist = c.get(f"/api/applications/{app_id}/history").get_json()
        assert hist[0]["source"] == "email" and hist[0]["new_status"] == "rejected"

    def test_dismiss_suggestion(self, client):
        c, _, db = client
        app_id, eid = self._seed(db)
        assert c.post(f"/api/email/messages/{eid}/dismiss").status_code == 200
        assert db.get_application(app_id).status == "applied"
        assert db.get_email_message(eid)["action"] == "dismissed"

    def test_unknown_ids_404(self, client):
        c, _, _ = client
        assert c.post("/api/email/messages/999/apply").status_code == 404
        assert c.post("/api/email/messages/999/dismiss").status_code == 404
        assert c.get("/api/applications/999/history").status_code == 404

    def test_manual_patch_records_history(self, client):
        c, _, db = client
        app_id, _ = self._seed(db)
        assert c.patch(f"/api/applications/{app_id}", json={"status": "interview"}).status_code == 200
        hist = c.get(f"/api/applications/{app_id}/history").get_json()
        assert hist[0]["source"] == "manual" and hist[0]["old_status"] == "applied" and hist[0]["new_status"] == "interview"
        # no-op patch (same status) adds no history row
        c.patch(f"/api/applications/{app_id}", json={"notes": "hi"})
        assert len(c.get(f"/api/applications/{app_id}/history").get_json()) == 1
