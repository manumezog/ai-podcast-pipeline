"""
Notifier — sends pipeline status emails via Gmail SMTP and pushes
status/latest.json to GitHub so the monitoring routine can read it.

Dependencies: all stdlib (smtplib, email, json, base64, urllib) — no new packages.

Gmail setup: create an App Password at myaccount.google.com/apppasswords
(requires 2FA). Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env.

GitHub setup: GITHUB_TOKEN must have contents:write on GITHUB_PIPELINE_REPO.
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def send_email(subject: str, body: str, cfg: Any) -> None:
    """Send a plain-text email via Gmail SMTP SSL."""
    if not cfg.gmail_address or not cfg.gmail_app_password:
        logger.warning("Gmail credentials not configured — skipping email.")
        return
    if not cfg.notification_email:
        logger.warning("NOTIFICATION_EMAIL not set — skipping email.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.gmail_address
    msg["To"] = cfg.notification_email

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(cfg.gmail_address, cfg.gmail_app_password)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", cfg.notification_email, subject)
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)


def _github_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return {}
        raise


def _redact_secrets(text: str, cfg: Any) -> str:
    """Replace known secret values with REDACTED so they can't leak via GitHub."""
    sensitive = [
        cfg.google_api_key, cfg.newsapi_key, cfg.github_token,
        cfg.gmail_app_password, cfg.cloudflare_r2_secret_access_key,
        cfg.reddit_client_secret, cfg.f5_api_key,
        getattr(cfg, "gmail_address", ""),
    ]
    for secret in sensitive:
        if secret and len(secret) > 4:
            text = text.replace(secret, "***REDACTED***")
    return text


def push_status_to_github(status: dict, cfg: Any) -> None:
    """Upsert status/latest.json in GITHUB_PIPELINE_REPO via the GitHub Contents API."""
    if not cfg.github_token or not cfg.github_pipeline_repo:
        logger.warning("GITHUB_TOKEN or GITHUB_PIPELINE_REPO not set — skipping status push.")
        return

    file_path = "status/latest.json"
    api_path = f"/repos/{cfg.github_pipeline_repo}/contents/{file_path}"
    content_b64 = base64.b64encode(
        json.dumps(status, indent=2, ensure_ascii=False).encode()
    ).decode()

    existing = _github_api("GET", api_path, cfg.github_token)
    sha = existing.get("sha")

    payload: dict = {
        "message": f"chore: update pipeline status {status.get('episode_date', '')}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    try:
        _github_api("PUT", api_path, cfg.github_token, payload)
        logger.info("status/latest.json pushed to %s.", cfg.github_pipeline_repo)
    except Exception as exc:
        logger.error("Failed to push status to GitHub: %s", exc)


def read_log_tail(cfg: Any, chars: int = 3000) -> str:
    """Return the last `chars` characters of pipeline.log."""
    log_path = Path(cfg.logs_dir) / "pipeline.log"
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return text[-chars:] if len(text) > chars else text
    except Exception:
        return ""


def notify_success(run_date: str, episode_path: str, duration_seconds: float, cfg: Any) -> None:
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)
    duration_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    now = datetime.now(timezone.utc).isoformat()

    # GitHub gets minimal public metadata only — no log content, no paths
    push_status_to_github({
        "last_run": now,
        "success": True,
        "exit_code": 0,
        "episode_date": run_date,
        "duration_seconds": duration_seconds,
    }, cfg)

    subject = f"[OK] {cfg.show_name} — Episode {run_date} ready"
    body = (
        f"Episode {run_date} produced successfully.\n\n"
        f"  Duration : {duration_str}\n"
        f"  Audio    : {episode_path}\n\n"
        f"Pipeline completed at {now}."
    )
    send_email(subject, body, cfg)


def notify_failure(run_date: str, error: str, cfg: Any) -> None:
    log_tail = read_log_tail(cfg)
    now = datetime.now(timezone.utc).isoformat()

    # GitHub gets minimal metadata — error/log stay email-only to avoid leaking secrets
    push_status_to_github({
        "last_run": now,
        "success": False,
        "exit_code": 1,
        "episode_date": run_date,
    }, cfg)

    # Redact secrets before including in email body
    safe_error = _redact_secrets(error, cfg)
    safe_log = _redact_secrets(_extract_error_context(log_tail), cfg)

    subject = f"[FAIL] {cfg.show_name} — Pipeline failed ({run_date})"
    body = (
        f"The pipeline failed for episode {run_date}.\n\n"
        f"Error: {safe_error}\n\n"
        f"--- Recent log ---\n{safe_log}"
    )
    send_email(subject, body, cfg)


def _extract_error_context(log_tail: str, lines: int = 30) -> str:
    """Return the last N lines of the log, prioritising ERROR lines."""
    all_lines = log_tail.splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return "\n".join(tail)
