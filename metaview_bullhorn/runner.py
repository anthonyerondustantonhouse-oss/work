"""Component six: one sync run, with logging and failure behaviour.

Order of a run:
  1. Read every conversation from Metaview up front. Any extraction error
     aborts the whole run before anything is matched or written.
  2. For each conversation not yet in the `processed` table: match, infer the
     action type, draft the note, and insert it as pending (or confirmed, only
     for an exact-email match when REQUIRE_CONFIRMATION_FOR_ALL is false).
  3. Write every item in status confirmed. A write failure marks that item
     failed and aborts the run.
  4. Record the run; after N consecutive failed runs, send an alert.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, ContextManager

from .alerts import send_alert
from .bullhorn import BullhornClient
from .config import Settings
from .errors import NoteGenerationError, WriteRefused
from .matcher import match_conversation
from .models import CONFIDENCE_HIGH, Conversation
from .note_writer import NoteGenerator, infer_action_type, validate_note
from .store import STATUS_CONFIRMED, STATUS_PENDING, Store

log = logging.getLogger(__name__)
run_log = logging.getLogger("metaview_bullhorn.runlog")


@dataclass
class RunStats:
    found: int = 0
    new: int = 0
    matched: int = 0
    queued: int = 0
    written: int = 0
    errors: int = 0
    ok: bool = False
    error_message: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("decisions", None)
        return data

    def summary(self) -> str:
        return (
            f"ok={self.ok} found={self.found} new={self.new} matched={self.matched} "
            f"queued={self.queued} written={self.written} errors={self.errors}"
            + (f" error={self.error_message!r}" if self.error_message else "")
        )


def write_item(store: Store, bullhorn: BullhornClient, item: dict[str, Any]) -> int:
    """Write one confirmed item to Bullhorn and mark it written. Returns the note id.

    Refuses anything that is not confirmed, has no record, or already has a
    note id: this is the last line of defence against double writes.
    """
    conv_id = item["conversation_id"]
    if item["status"] != STATUS_CONFIRMED:
        raise WriteRefused(f"{conv_id} is {item['status']}, not confirmed")
    if item.get("note_id"):
        raise WriteRefused(f"{conv_id} already has note {item['note_id']}")
    record = item["proposed_record"]
    if record is None:
        raise WriteRefused(f"{conv_id} has no Bullhorn record")
    note = validate_note(item.get("draft_note") or "")
    action = item.get("action_type") or "Note"
    note_id = bullhorn.create_note(record, note, action, item.get("job_order_id"))
    store.mark_written(conv_id, note_id)
    log.info("written: conversation %s -> %s note %s", conv_id, record.ref, note_id)
    return note_id


def write_confirmed(store: Store, bullhorn: BullhornClient, stats: RunStats | None = None) -> int:
    """Write every confirmed item. Stops at the first failure (after marking it failed)."""
    stats = stats or RunStats()
    written = 0
    for item in store.confirmed():
        conv_id = item["conversation_id"]
        try:
            write_item(store, bullhorn, item)
        except Exception as exc:
            store.mark_failed(conv_id, f"{type(exc).__name__}: {exc}")
            stats.errors += 1
            raise
        written += 1
        stats.written += 1
    return written


def process_new_conversation(
    conversation: Conversation,
    store: Store,
    bullhorn: BullhornClient,
    generator: NoteGenerator,
    settings: Settings,
    stats: RunStats,
    dry_run: bool = False,
) -> dict[str, Any]:
    match = match_conversation(conversation, bullhorn, settings)
    action_type = infer_action_type(conversation, settings)
    if match.proposed is not None:
        stats.matched += 1

    draft = ""
    error: str | None = None
    try:
        draft = generator.generate(conversation, action_type)
    except NoteGenerationError as exc:
        error = str(exc)
        stats.errors += 1
        log.warning("conversation %s: %s", conversation.id, exc)

    auto_confirm = (
        match.confidence == CONFIDENCE_HIGH
        and not settings.require_confirmation_for_all
        and match.proposed is not None
        and draft
        and error is None
    )
    status = STATUS_CONFIRMED if auto_confirm else STATUS_PENDING
    if status == STATUS_PENDING:
        stats.queued += 1

    decision = {
        "conversation_id": conversation.id,
        "title": conversation.title,
        "confidence": match.confidence,
        "reason": match.reason,
        "proposed": match.proposed.ref if match.proposed else None,
        "action_type": action_type,
        "status": status,
        "draft": draft,
        "error": error,
    }
    if not dry_run:
        store.add_new(conversation, match, draft, action_type, status, error=error)
    log.info(
        "conversation %s (%s): confidence=%s proposed=%s status=%s",
        conversation.id, conversation.title, match.confidence, decision["proposed"], status,
    )
    return decision


def run_once(
    settings: Settings,
    store: Store,
    bullhorn: BullhornClient,
    generator: NoteGenerator,
    reader_factory: Callable[[], ContextManager[Any]],
    dry_run: bool = False,
) -> RunStats:
    """Execute one full cycle. Never raises; the outcome is in the returned RunStats."""
    stats = RunStats()
    run_id = None if dry_run else store.start_run()
    try:
        with reader_factory() as reader:
            conversations: list[Conversation] = reader.read_all()
        stats.found = len(conversations)

        # Dedup: anything already in `processed` (written, pending, confirmed,
        # skipped or failed) is skipped silently; only unseen ids are processed.
        new = [c for c in conversations if not store.is_known(c.id)]
        stats.new = len(new)
        for conversation in new:
            stats.decisions.append(
                process_new_conversation(conversation, store, bullhorn, generator, settings, stats, dry_run=dry_run)
            )

        if dry_run:
            pending_writes = len(store.confirmed())
            if pending_writes:
                log.info("dry run: %d confirmed item(s) would be written", pending_writes)
        else:
            write_confirmed(store, bullhorn, stats)

        stats.ok = stats.errors == 0
    except Exception as exc:  # noqa: BLE001 - every failure must be logged, never raised out of a run
        stats.ok = False
        stats.error_message = f"{type(exc).__name__}: {exc}"
        stats.errors += 1
        log.error("run aborted: %s\n%s", stats.error_message, traceback.format_exc())

    if not dry_run and run_id is not None:
        store.finish_run(run_id, stats.ok, stats.as_dict(), stats.error_message)
    run_log.info("RUN %s%s", "(dry) " if dry_run else "", stats.summary())

    if not dry_run:
        _maybe_alert(settings, store, stats)
    return stats


def _maybe_alert(settings: Settings, store: Store, stats: RunStats) -> None:
    failures = store.consecutive_failures()
    threshold = settings.consecutive_failure_alert_threshold
    if threshold > 0 and failures >= threshold:
        subject = f"Metaview→Bullhorn sync: {failures} consecutive failed runs"
        body = f"The last {failures} runs failed. Latest: {stats.summary()}\nLog: {settings.log_path}"
        log.error(subject)
        send_alert(settings, subject, body)
