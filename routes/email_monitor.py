"""Employer email monitoring routes (IMAP, read-only).

The IMAP password is write-only: accepted on PUT/test, stored in the OS keyring,
and never returned by any endpoint (only ``has_password``).
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

import app_state
from config.settings import (
    EmailConfig,
    delete_email_password,
    get_email_password,
    load_config,
    save_config,
    set_email_password,
)
from core.email_monitor import (
    PROVIDER_HOSTS,
    EmailMonitor,
    EmailMonitorError,
    EmailMonitorService,
)
from core.i18n import t

logger = logging.getLogger(__name__)

email_bp = Blueprint("email_monitor", __name__)

VALID_PROVIDERS = set(PROVIDER_HOSTS) | {"custom"}


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------

def _emit_status_update(payload: dict) -> None:
    sio = app_state.socketio
    if sio is not None:
        sio.emit("application_status_changed", payload)


def _build_monitor() -> EmailMonitor:
    return EmailMonitor(
        db=app_state.db,
        get_config=load_config,
        get_password=get_email_password,
        on_status_update=_emit_status_update,
    )


def _get_interval() -> int:
    cfg = load_config()
    return cfg.email.poll_interval_minutes if cfg else 15


def get_service() -> EmailMonitorService:
    if app_state.email_service is None:
        app_state.email_service = EmailMonitorService(_build_monitor(), _get_interval)
    return app_state.email_service


def init_email_service() -> None:
    """Called from create_app(): start polling only if the user enabled it."""
    cfg = load_config()
    if cfg is not None and cfg.email.enabled and get_email_password():
        get_service().start()


def _sync_service(cfg: EmailConfig) -> None:
    service = get_service()
    if cfg.enabled and get_email_password():
        service.start()
    else:
        service.stop()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(data: dict) -> tuple[dict, str | None]:
    """Return (clean_fields, error_message)."""
    clean: dict = {}
    if "enabled" in data:
        clean["enabled"] = bool(data["enabled"])
    if "auto_update_status" in data:
        clean["auto_update_status"] = bool(data["auto_update_status"])
    if "provider" in data:
        if data["provider"] not in VALID_PROVIDERS:
            return {}, t("errors.email_invalid_provider")
        clean["provider"] = data["provider"]
    for key in ("imap_host", "username", "folder"):
        if key in data:
            val = data[key]
            if not isinstance(val, str) or len(val) > 255:
                return {}, t("errors.email_invalid_field", field=key)
            clean[key] = val.strip()
    if "imap_host" in clean and any(c in clean["imap_host"] for c in " /\\:@"):
        return {}, t("errors.email_invalid_field", field="imap_host")
    if "imap_port" in data:
        try:
            port = int(data["imap_port"])
        except (TypeError, ValueError):
            return {}, t("errors.email_invalid_field", field="imap_port")
        if not 1 <= port <= 65535:
            return {}, t("errors.email_invalid_field", field="imap_port")
        clean["imap_port"] = port
    for key, lo, hi in (("poll_interval_minutes", 5, 1440), ("lookback_days", 1, 90)):
        if key in data:
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                return {}, t("errors.email_invalid_field", field=key)
            if not lo <= val <= hi:
                return {}, t("errors.email_invalid_field", field=key)
            clean[key] = val
    if "min_confidence" in data:
        try:
            conf = float(data["min_confidence"])
        except (TypeError, ValueError):
            return {}, t("errors.email_invalid_field", field="min_confidence")
        if not 0.5 <= conf <= 1.0:
            return {}, t("errors.email_invalid_field", field="min_confidence")
        clean["min_confidence"] = conf
    return clean, None


def _public_config(cfg: EmailConfig) -> dict:
    out = cfg.model_dump()
    out["has_password"] = bool(get_email_password())
    out["providers"] = sorted(VALID_PROVIDERS)
    out["status"] = get_service().status() if app_state.email_service else {
        "running": False, "last_check_at": None, "last_error": None, "last_result": None,
    }
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@email_bp.route("/api/email/config", methods=["GET"])
def get_email_config():
    config = load_config()
    cfg = config.email if config else EmailConfig()
    return jsonify(_public_config(cfg))


@email_bp.route("/api/email/config", methods=["PUT"])
def update_email_config():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"error": t("errors.request_body_required")}), 400
    config = load_config()
    if config is None:
        return jsonify({"error": t("errors.config_not_found")}), 400

    clean, err = _validate(data)
    if err:
        return jsonify({"error": err}), 400

    password = data.get("password")
    if password is not None and not isinstance(password, str):
        return jsonify({"error": t("errors.email_invalid_field", field="password")}), 400

    merged = config.email.model_copy(update=clean)
    if merged.enabled:
        if not merged.username:
            return jsonify({"error": t("errors.email_username_required")}), 400
        if merged.provider == "custom" and not merged.imap_host:
            return jsonify({"error": t("errors.email_host_required")}), 400
        if not (password or get_email_password()):
            return jsonify({"error": t("errors.email_password_required")}), 400

    if data.get("clear_password"):
        delete_email_password()
        merged = merged.model_copy(update={"enabled": False})
    elif password:
        set_email_password(password)

    config.email = merged
    save_config(config)
    _sync_service(merged)
    return jsonify(_public_config(merged))


@email_bp.route("/api/email/test", methods=["POST"])
def test_email_connection():
    """Try to log in and open the folder using saved settings plus any overrides."""
    data = request.get_json(silent=True) or {}
    config = load_config()
    base = config.email if config else EmailConfig()
    clean, err = _validate({k: v for k, v in data.items() if k != "password"})
    if err:
        return jsonify({"error": err}), 400
    cfg = base.model_copy(update=clean)
    password = data.get("password") if isinstance(data.get("password"), str) and data.get("password") else None

    try:
        monitor = _build_monitor()
        with monitor.open_session(cfg, password=password) as session:
            count = session.message_count
    except EmailMonitorError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "message_count": count})


@email_bp.route("/api/email/check", methods=["POST"])
def check_email_now():
    config = load_config()
    if config is None or not config.email.enabled:
        return jsonify({"error": t("errors.email_not_enabled")}), 409
    try:
        result = get_service().check_now()
    except EmailMonitorError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@email_bp.route("/api/email/messages", methods=["GET"])
def list_email_messages():
    db = app_state.db
    application_id = request.args.get("application_id", type=int)
    action = request.args.get("action")
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    return jsonify(db.get_email_messages(application_id=application_id, action=action, limit=limit))


@email_bp.route("/api/email/messages/<int:email_id>/apply", methods=["POST"])
def apply_email_suggestion(email_id: int):
    row = app_state.db.get_email_message(email_id)
    if row is None:
        return jsonify({"error": t("errors.not_found")}), 404
    if not get_service()._monitor.accept_suggestion(email_id):
        return jsonify({"error": t("errors.email_suggestion_stale")}), 409
    return jsonify({"success": True})


@email_bp.route("/api/email/messages/<int:email_id>/dismiss", methods=["POST"])
def dismiss_email_suggestion(email_id: int):
    row = app_state.db.get_email_message(email_id)
    if row is None:
        return jsonify({"error": t("errors.not_found")}), 404
    if row["action"] == "suggested":
        app_state.db.set_email_action(email_id, "dismissed")
    return jsonify({"success": True})
