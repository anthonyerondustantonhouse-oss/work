"""Settings loaded from the environment / a .env file. No credentials live in
code; everything sensitive comes from the env file, which is git-ignored."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_ACTION_MAP = {
    "interview": "Interview",
    "intake": "Client Call",
    "brief": "Client Call",
    "client": "Client Call",
    "screen": "Candidate Call",
    "catch up": "Candidate Call",
    "catch-up": "Candidate Call",
    "check in": "Candidate Call",
    "offer": "Offer",
}


def load_env_file(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE pairs into os.environ without overriding existing values.

    Uses python-dotenv when installed; otherwise a minimal parser that handles
    comments, blank lines, and single/double quoted values.
    """
    env_path = Path(path) if path else Path(".env")
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int, name: str) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    # Bullhorn
    bullhorn_client_id: str
    bullhorn_client_secret: str
    bullhorn_username: str
    bullhorn_password: str
    bullhorn_auth_url: str
    bullhorn_rest_token_url: str
    bullhorn_rest_login_url: str
    bullhorn_test_record: str | None

    # Metaview
    metaview_base_url: str
    metaview_conversations_path: str
    playwright_profile_dir: Path
    metaview_selectors_file: Path | None
    metaview_max_conversations: int
    playwright_headless: bool

    # Identity
    internal_email_domains: list[str]
    internal_emails: list[str]
    internal_names: list[str]

    # Note generation
    anthropic_api_key: str | None
    note_model: str
    note_action_map: dict[str, str]
    note_default_action: str

    # Confirmation gate
    require_confirmation_for_all: bool
    write_on_approve: bool
    confirm_host: str
    confirm_port: int

    # State / logging / schedule
    db_path: Path
    log_path: Path
    sync_interval_minutes: int

    # Alerts
    consecutive_failure_alert_threshold: int
    alert_desktop: bool
    alert_email_to: str | None
    alert_smtp_host: str | None
    alert_smtp_port: int
    alert_smtp_username: str | None
    alert_smtp_password: str | None
    alert_smtp_from: str | None

    extra: dict = field(default_factory=dict)

    @property
    def metaview_conversations_url(self) -> str:
        return self.metaview_base_url.rstrip("/") + "/" + self.metaview_conversations_path.lstrip("/")

    @property
    def email_alerts_enabled(self) -> bool:
        return bool(self.alert_email_to and self.alert_smtp_host)


REQUIRED = [
    "BULLHORN_CLIENT_ID",
    "BULLHORN_CLIENT_SECRET",
    "BULLHORN_USERNAME",
    "BULLHORN_PASSWORD",
    "BULLHORN_REST_TOKEN_URL",
    "METAVIEW_BASE_URL",
    "PLAYWRIGHT_PROFILE_DIR",
]


def load_settings(env_file: str | os.PathLike | None = None, environ: dict | None = None) -> Settings:
    """Build Settings from the environment, loading the .env file first.

    Raises ConfigError naming every missing required variable at once.
    """
    if environ is None:
        load_env_file(env_file)
        environ = os.environ
    env = environ.get

    missing = [name for name in REQUIRED if not (env(name) or "").strip()]
    if missing:
        raise ConfigError("Missing required settings: " + ", ".join(missing) + ". Copy .env.example to .env and fill them in.")

    action_map = dict(DEFAULT_ACTION_MAP)
    raw_map = env("NOTE_ACTION_MAP")
    if raw_map and raw_map.strip():
        try:
            parsed = json.loads(raw_map)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"NOTE_ACTION_MAP is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ConfigError("NOTE_ACTION_MAP must be a JSON object of string keyword -> string action")
        action_map = {k.lower(): v for k, v in parsed.items()}

    selectors_file = (env("METAVIEW_SELECTORS_FILE") or "").strip()
    test_record = (env("BULLHORN_TEST_RECORD") or "").strip() or None
    if test_record:
        parse_record_ref(test_record)  # validate early

    return Settings(
        bullhorn_client_id=env("BULLHORN_CLIENT_ID").strip(),
        bullhorn_client_secret=env("BULLHORN_CLIENT_SECRET").strip(),
        bullhorn_username=env("BULLHORN_USERNAME").strip(),
        bullhorn_password=env("BULLHORN_PASSWORD"),
        bullhorn_auth_url=(env("BULLHORN_AUTH_URL") or "https://auth.bullhornstaffing.com/oauth/authorize").strip(),
        bullhorn_rest_token_url=env("BULLHORN_REST_TOKEN_URL").strip(),
        bullhorn_rest_login_url=(env("BULLHORN_REST_LOGIN_URL") or "https://rest.bullhornstaffing.com/rest-services/login").strip(),
        bullhorn_test_record=test_record,
        metaview_base_url=env("METAVIEW_BASE_URL").strip(),
        metaview_conversations_path=(env("METAVIEW_CONVERSATIONS_PATH") or "/conversations").strip(),
        playwright_profile_dir=Path(env("PLAYWRIGHT_PROFILE_DIR").strip()).expanduser(),
        metaview_selectors_file=Path(selectors_file).expanduser() if selectors_file else None,
        metaview_max_conversations=_int(env("METAVIEW_MAX_CONVERSATIONS"), 25, "METAVIEW_MAX_CONVERSATIONS"),
        playwright_headless=_bool(env("PLAYWRIGHT_HEADLESS"), True),
        internal_email_domains=_csv(env("INTERNAL_EMAIL_DOMAINS")),
        internal_emails=_csv(env("INTERNAL_EMAILS")),
        internal_names=_csv(env("INTERNAL_NAMES")),
        anthropic_api_key=(env("ANTHROPIC_API_KEY") or "").strip() or None,
        note_model=(env("NOTE_MODEL") or "claude-opus-5").strip(),
        note_action_map=action_map,
        note_default_action=(env("NOTE_DEFAULT_ACTION") or "Call").strip(),
        require_confirmation_for_all=_bool(env("REQUIRE_CONFIRMATION_FOR_ALL"), True),
        write_on_approve=_bool(env("WRITE_ON_APPROVE"), False),
        confirm_host=(env("CONFIRM_HOST") or "127.0.0.1").strip(),
        confirm_port=_int(env("CONFIRM_PORT"), 8765, "CONFIRM_PORT"),
        db_path=Path((env("DB_PATH") or "./data/sync.db").strip()).expanduser(),
        log_path=Path((env("LOG_PATH") or "./data/sync.log").strip()).expanduser(),
        sync_interval_minutes=_int(env("SYNC_INTERVAL_MINUTES"), 30, "SYNC_INTERVAL_MINUTES"),
        consecutive_failure_alert_threshold=_int(env("CONSECUTIVE_FAILURE_ALERT_THRESHOLD"), 3, "CONSECUTIVE_FAILURE_ALERT_THRESHOLD"),
        alert_desktop=_bool(env("ALERT_DESKTOP"), True),
        alert_email_to=(env("ALERT_EMAIL_TO") or "").strip() or None,
        alert_smtp_host=(env("ALERT_SMTP_HOST") or "").strip() or None,
        alert_smtp_port=_int(env("ALERT_SMTP_PORT"), 587, "ALERT_SMTP_PORT"),
        alert_smtp_username=(env("ALERT_SMTP_USERNAME") or "").strip() or None,
        alert_smtp_password=env("ALERT_SMTP_PASSWORD") or None,
        alert_smtp_from=(env("ALERT_SMTP_FROM") or "").strip() or None,
    )


ENTITIES = ("Candidate", "ClientContact")


def parse_record_ref(ref: str) -> tuple[str, int]:
    """Parse 'Candidate:123' into ('Candidate', 123)."""
    entity, _, raw_id = ref.strip().partition(":")
    entity = entity.strip()
    if entity not in ENTITIES:
        raise ConfigError(f"Record reference {ref!r} must start with one of {ENTITIES}")
    try:
        record_id = int(raw_id.strip())
    except ValueError as exc:
        raise ConfigError(f"Record reference {ref!r} must end with a numeric id") from exc
    if record_id <= 0:
        raise ConfigError(f"Record reference {ref!r} must have a positive id")
    return entity, record_id
