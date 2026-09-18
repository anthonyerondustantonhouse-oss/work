"""Alerts for repeated failures. Silent failure is the worst outcome, so this
tries every configured channel and logs when a channel itself fails."""

from __future__ import annotations

import logging
import platform
import shutil
import smtplib
import subprocess
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger(__name__)


def send_alert(settings: Settings, subject: str, body: str) -> list[str]:
    """Send on every enabled channel. Returns the names of channels that succeeded."""
    delivered: list[str] = []
    if settings.email_alerts_enabled:
        try:
            _send_email(settings, subject, body)
            delivered.append("email")
        except Exception as exc:  # noqa: BLE001
            log.error("email alert failed: %s", exc)
    if settings.alert_desktop:
        try:
            if _send_desktop(subject, body):
                delivered.append("desktop")
        except Exception as exc:  # noqa: BLE001
            log.error("desktop alert failed: %s", exc)
    if not delivered:
        log.error("ALERT (no channel delivered): %s - %s", subject, body)
    return delivered


def _send_email(settings: Settings, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.alert_smtp_from or settings.alert_smtp_username or settings.alert_email_to
    msg["To"] = settings.alert_email_to
    msg.set_content(body)
    assert settings.alert_smtp_host
    with smtplib.SMTP(settings.alert_smtp_host, settings.alert_smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if settings.alert_smtp_port != 25:
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        if settings.alert_smtp_username and settings.alert_smtp_password:
            smtp.login(settings.alert_smtp_username, settings.alert_smtp_password)
        smtp.send_message(msg)


def _send_desktop(subject: str, body: str) -> bool:
    system = platform.system()
    if system == "Darwin":
        script = f'display notification "{_esc(body)}" with title "{_esc(subject)}"'
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
        return True
    if system == "Linux" and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "critical", subject, body], check=True, timeout=10)
        return True
    if system == "Windows":
        ps = (
            "[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
            f"[System.Windows.Forms.MessageBox]::Show('{_esc(body)}','{_esc(subject)}')"
        )
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        return True
    log.warning("no desktop notification method available on %s", system)
    return False


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")
