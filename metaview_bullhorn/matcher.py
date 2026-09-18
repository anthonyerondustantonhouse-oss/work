"""Component three: map a conversation to a Bullhorn record.

Rules, in priority order:
  * Exact email match is the ONLY high-confidence path, and only when exactly
    one distinct record matches across all external attendees.
  * More than one email hit: low confidence, all hits offered as alternatives.
  * No email or no email hit: fall back to a name search. Every name-based
    result is low confidence regardless of how good it looks.
  * Nothing found: confidence "none"; the item still queues so a human can
    assign a record by hand.

Attendees that belong to the firm (internal domains, addresses or names) are
never looked up: we want the candidate or client, not ourselves.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import Settings
from .models import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_NONE, Attendee, Conversation, MatchResult, RecordRef

log = logging.getLogger(__name__)


class RecordSource(Protocol):
    def search_by_email(self, email: str) -> list[RecordRef]: ...
    def search_by_name(self, full_name: str, limit: int = 10) -> list[RecordRef]: ...
    def job_order_suggestions(self, record: RecordRef, limit: int = 10) -> list[dict]: ...


def is_internal(attendee: Attendee, settings: Settings) -> bool:
    email = (attendee.email or "").strip().lower()
    if email:
        if email in settings.internal_emails:
            return True
        domain = email.rsplit("@", 1)[-1]
        if domain in settings.internal_email_domains:
            return True
    name = attendee.name.strip().lower()
    return bool(name) and name in settings.internal_names


def external_attendees(conversation: Conversation, settings: Settings) -> list[Attendee]:
    return [a for a in conversation.attendees if not is_internal(a, settings)]


def _dedupe(records: list[RecordRef]) -> list[RecordRef]:
    seen: set[str] = set()
    out: list[RecordRef] = []
    for record in records:
        if record.ref not in seen:
            seen.add(record.ref)
            out.append(record)
    return out


def match_conversation(conversation: Conversation, source: RecordSource, settings: Settings) -> MatchResult:
    externals = external_attendees(conversation, settings)
    if not externals:
        return MatchResult(
            confidence=CONFIDENCE_NONE,
            reason="No external attendee: every attendee matched the internal domain/name list.",
            proposed=None,
        )

    # --- exact email path ---------------------------------------------------
    email_hits: list[RecordRef] = []
    emails_tried: list[str] = []
    for attendee in externals:
        if not attendee.email:
            continue
        emails_tried.append(attendee.email)
        email_hits.extend(source.search_by_email(attendee.email))
    email_hits = _dedupe(email_hits)

    if len(email_hits) == 1:
        record = email_hits[0]
        return MatchResult(
            confidence=CONFIDENCE_HIGH,
            reason=f"Exactly one Bullhorn record has the attendee email {record.email}.",
            proposed=record,
            alternatives=[],
            job_orders=_safe_job_orders(source, record),
        )
    if len(email_hits) > 1:
        return MatchResult(
            confidence=CONFIDENCE_LOW,
            reason=f"{len(email_hits)} Bullhorn records share the attendee email(s) {', '.join(emails_tried)}; pick the right one.",
            proposed=email_hits[0],
            alternatives=email_hits[1:],
            job_orders=_safe_job_orders(source, email_hits[0]),
        )

    # --- name fallback (always low confidence) ------------------------------
    name_hits: list[RecordRef] = []
    for attendee in externals:
        name_hits.extend(source.search_by_name(attendee.name))
    name_hits = _dedupe(name_hits)

    why_no_email = (
        f"no Bullhorn record has the email(s) {', '.join(emails_tried)}" if emails_tried else "Metaview exposed no email for the attendee(s)"
    )
    names = ", ".join(a.name for a in externals)
    if name_hits:
        return MatchResult(
            confidence=CONFIDENCE_LOW,
            reason=f"Name search only ({why_no_email}); {len(name_hits)} record(s) match the name(s) {names}. Name matches are never trusted without confirmation.",
            proposed=name_hits[0],
            alternatives=name_hits[1:],
            job_orders=_safe_job_orders(source, name_hits[0]),
        )
    return MatchResult(
        confidence=CONFIDENCE_NONE,
        reason=f"No match: {why_no_email}, and no record matches the name(s) {names}.",
        proposed=None,
    )


def _safe_job_orders(source: RecordSource, record: RecordRef) -> list[dict]:
    try:
        return source.job_order_suggestions(record)
    except Exception as exc:  # noqa: BLE001 - suggestions are optional
        log.warning("job order suggestions failed for %s: %s", record.ref, exc)
        return []
