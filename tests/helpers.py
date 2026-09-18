"""Shared fakes for the tests. No network, no browser, no third-party packages."""

from __future__ import annotations

from metaview_bullhorn.config import Settings, load_settings
from metaview_bullhorn.models import Attendee, Conversation, RecordRef

BASE_ENV = {
    "BULLHORN_CLIENT_ID": "cid",
    "BULLHORN_CLIENT_SECRET": "secret",
    "BULLHORN_USERNAME": "api.user",
    "BULLHORN_PASSWORD": "pw",
    "BULLHORN_REST_TOKEN_URL": "https://auth.example/oauth/token",
    "BULLHORN_AUTH_URL": "https://auth.example/oauth/authorize",
    "BULLHORN_REST_LOGIN_URL": "https://rest.example/rest-services/login",
    "METAVIEW_BASE_URL": "https://app.metaview.test",
    "PLAYWRIGHT_PROFILE_DIR": "/tmp/mv-profile-test",
    "INTERNAL_EMAIL_DOMAINS": "ourfirm.com",
    "INTERNAL_NAMES": "Anthony Erondu",
    "ALERT_DESKTOP": "false",
    "DB_PATH": ":memory:",
    "LOG_PATH": "/tmp/mv-test.log",
}


def make_settings(**overrides: str) -> Settings:
    env = dict(BASE_ENV)
    env.update(overrides)
    return load_settings(environ=env)


def make_conversation(
    conv_id: str = "abc123",
    title: str = "Candidate screen - Jane Doe",
    attendees: list[Attendee] | None = None,
    body: str = "Jane talked about her current role and what she wants next. Agreed to speak again on Friday.",
) -> Conversation:
    return Conversation(
        id=conv_id,
        url=f"https://app.metaview.test/conversations/{conv_id}",
        title=title,
        occurred_at="2026-09-17T10:00+00:00",
        attendees=attendees or [Attendee("Anthony Erondu", "anthony@ourfirm.com"), Attendee("Jane Doe", "jane@example.com")],
        body=body,
        body_source="notes",
    )


def record(entity: str = "Candidate", record_id: int = 101, name: str = "Jane Doe", email: str | None = "jane@example.com") -> RecordRef:
    return RecordRef(entity=entity, id=record_id, name=name, email=email, detail="Active")


class FakeBullhorn:
    """Stands in for BullhornClient in matcher/runner tests."""

    def __init__(self, by_email: dict[str, list[RecordRef]] | None = None, by_name: dict[str, list[RecordRef]] | None = None):
        self.by_email = {k.lower(): v for k, v in (by_email or {}).items()}
        self.by_name = {k.lower(): v for k, v in (by_name or {}).items()}
        self.notes: list[dict] = []
        self.fail_writes = False
        self.next_note_id = 5000
        self.records: dict[str, RecordRef] = {}

    def search_by_email(self, email: str) -> list[RecordRef]:
        return list(self.by_email.get(email.lower(), []))

    def search_by_name(self, full_name: str, limit: int = 10) -> list[RecordRef]:
        return list(self.by_name.get(full_name.lower(), []))

    def job_order_suggestions(self, rec: RecordRef, limit: int = 10) -> list[dict]:
        return [{"id": 77, "title": "Head of Sales", "company": "Acme"}]

    def get_record(self, entity: str, record_id: int) -> RecordRef:
        key = f"{entity}:{record_id}"
        if key not in self.records:
            from metaview_bullhorn.errors import BullhornError

            raise BullhornError(f"{key} not found")
        return self.records[key]

    def create_note(self, rec: RecordRef, comments: str, action: str, job_order_id: int | None = None) -> int:
        if self.fail_writes:
            from metaview_bullhorn.errors import BullhornError

            raise BullhornError("simulated write failure")
        self.next_note_id += 1
        self.notes.append({"id": self.next_note_id, "record": rec.ref, "comments": comments, "action": action, "job_order_id": job_order_id})
        return self.next_note_id


class FakeGenerator:
    def __init__(self, note: str | None = "Discussed the role and her interest in moving. Next call booked for Friday.", error: str | None = None):
        self.note = note
        self.error = error
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    def generate(self, conversation: Conversation, action_type: str) -> str:
        self.calls += 1
        if self.error:
            from metaview_bullhorn.errors import NoteGenerationError

            raise NoteGenerationError(self.error)
        return self.note or ""


class FakeReader:
    """Context manager returning canned conversations, or raising on read."""

    def __init__(self, conversations: list[Conversation] | None = None, error: Exception | None = None):
        self.conversations = conversations or []
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_all(self, limit=None):
        if self.error:
            raise self.error
        return list(self.conversations)
