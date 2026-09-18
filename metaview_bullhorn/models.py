"""Plain data types shared across components."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class Attendee:
    name: str
    email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Conversation:
    """A fully extracted Metaview conversation. Every field is required to be
    present except attendee emails; the reader raises ExtractionError rather
    than constructing one of these with a blank field."""

    id: str
    url: str
    title: str
    occurred_at: str  # ISO 8601 when parseable, otherwise the raw text shown on the page
    attendees: list[Attendee]
    body: str  # Metaview's generated notes, or the transcript when no notes exist
    body_source: str = "notes"  # "notes" | "transcript"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @property
    def attendee_names(self) -> list[str]:
        return [a.name for a in self.attendees]


CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"


@dataclass
class RecordRef:
    """A Bullhorn person record as shown in the confirmation view."""

    entity: str  # Candidate | ClientContact
    id: int
    name: str
    email: str | None = None
    detail: str | None = None  # e.g. company for a contact, status for a candidate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RecordRef | None":
        if not data:
            return None
        return cls(
            entity=data["entity"],
            id=int(data["id"]),
            name=data.get("name") or "",
            email=data.get("email"),
            detail=data.get("detail"),
        )

    @property
    def ref(self) -> str:
        return f"{self.entity}:{self.id}"


@dataclass
class MatchResult:
    confidence: str  # high | low | none
    reason: str
    proposed: RecordRef | None
    alternatives: list[RecordRef] = field(default_factory=list)
    job_orders: list[dict[str, Any]] = field(default_factory=list)  # suggestions for the confirmation view
