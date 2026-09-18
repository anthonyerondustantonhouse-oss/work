"""Component four: the confirmation gate.

A single-page local web view (bound to localhost only) listing every queued
conversation with its proposed Bullhorn record, the match reasoning, any
alternatives, and the editable draft note. Approve, reject, or reassign.

Nothing reaches Bullhorn until an item is approved. Items sit in the queue
indefinitely; there is no timeout and no automatic write.
"""

from __future__ import annotations

import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from .config import ENTITIES, Settings, parse_record_ref
from .errors import NoteValidationError, SyncError
from .models import RecordRef
from .note_writer import validate_note
from .store import STATUS_FAILED, STATUS_PENDING, Store

log = logging.getLogger(__name__)

RecordLookup = Callable[[str, int], RecordRef]
ItemWriter = Callable[[str], int]  # conversation_id -> note id


class ConfirmationApp:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        record_lookup: RecordLookup | None = None,
        writer: ItemWriter | None = None,
    ):
        self.settings = settings
        self.store = store
        self.record_lookup = record_lookup
        self.writer = writer

    # ---- actions --------------------------------------------------------

    def approve(self, form: dict[str, str]) -> str:
        conv_id = form.get("id", "").strip()
        item = self.store.get(conv_id)
        if item is None:
            raise SyncError(f"unknown item {conv_id}")
        if item["status"] not in (STATUS_PENDING, STATUS_FAILED):
            raise SyncError(f"item is {item['status']}; only pending or failed items can be approved")

        record = self._resolve_record(item, form)
        try:
            note = validate_note(form.get("note", ""))
        except NoteValidationError as exc:
            raise SyncError(f"note rejected: {exc}") from exc
        action = (form.get("action") or "").strip()
        if not action:
            raise SyncError("action type is required")
        job_order_raw = (form.get("job_order_id") or "").strip()
        job_order_id: int | None = None
        if job_order_raw:
            try:
                job_order_id = int(job_order_raw)
            except ValueError as exc:
                raise SyncError("job order id must be a number") from exc
            if job_order_id <= 0:
                raise SyncError("job order id must be positive")

        self.store.confirm(conv_id, record, note, action, job_order_id)
        log.info("approved %s -> %s (action=%s, job order=%s)", conv_id, record.ref, action, job_order_id)
        if self.settings.write_on_approve and self.writer is not None:
            note_id = self.writer(conv_id)
            return f"Approved and written to Bullhorn as note {note_id} on {record.ref}."
        return f"Approved. {record.ref} will be written on the next run (or `mvsync write-confirmed`)."

    def _resolve_record(self, item: dict[str, Any], form: dict[str, str]) -> RecordRef:
        choice = (form.get("record") or "proposed").strip()
        if choice == "custom":
            entity = (form.get("custom_entity") or "").strip()
            raw_id = (form.get("custom_id") or "").strip()
            if entity not in ENTITIES or not raw_id:
                raise SyncError("reassign needs an entity (Candidate/ClientContact) and a record id")
            entity, record_id = parse_record_ref(f"{entity}:{raw_id}")
            if self.record_lookup is None:
                raise SyncError("reassign is unavailable: Bullhorn lookup is not configured for this server")
            return self.record_lookup(entity, record_id)  # raises if the record does not exist
        candidates = ([item["proposed_record"]] if item["proposed_record"] else []) + list(item["alternatives"])
        for candidate in candidates:
            if candidate and candidate.ref == choice:
                return candidate
        if choice == "proposed" and item["proposed_record"]:
            return item["proposed_record"]
        raise SyncError("no Bullhorn record selected; choose one of the options or reassign by id")

    def reject(self, form: dict[str, str]) -> str:
        conv_id = form.get("id", "").strip()
        self.store.skip(conv_id, reason="rejected in confirmation page")
        return f"Rejected {conv_id}; nothing will be written."

    def reopen(self, form: dict[str, str]) -> str:
        conv_id = form.get("id", "").strip()
        self.store.reopen(conv_id)
        return f"Reopened {conv_id}."

    # ---- rendering ------------------------------------------------------

    def render_index(self, message: str | None = None, error: str | None = None) -> str:
        items = self.store.queue()
        counts = self.store.counts()
        runs = self.store.recent_runs(5)
        parts = [
            "<!doctype html><html><head><meta charset='utf-8'><title>Metaview → Bullhorn queue</title>",
            "<style>", CSS, "</style></head><body>",
            "<h1>Metaview → Bullhorn confirmation queue</h1>",
        ]
        if message:
            parts.append(f"<p class='flash ok'>{html.escape(message)}</p>")
        if error:
            parts.append(f"<p class='flash err'>{html.escape(error)}</p>")
        parts.append(
            "<p class='counts'>"
            + " · ".join(f"{html.escape(k)}: {v}" for k, v in counts.items())
            + f" · confirmation required for all: {'yes' if self.settings.require_confirmation_for_all else 'exact-email matches auto-write'}"
            + "</p>"
        )
        if not items:
            parts.append("<p class='empty'>Queue is empty.</p>")
        for item in items:
            parts.append(self._render_item(item))
        parts.append("<h2>Recent runs</h2><table><tr><th>started</th><th>ok</th><th>found</th><th>new</th><th>matched</th><th>queued</th><th>written</th><th>errors</th><th>error</th></tr>")
        for run in runs:
            parts.append(
                "<tr>" + "".join(
                    f"<td>{html.escape(str(run.get(k) if run.get(k) is not None else ''))}</td>"
                    for k in ("started_at", "ok", "found", "new", "matched", "queued", "written", "errors", "error_message")
                ) + "</tr>"
            )
        parts.append("</table></body></html>")
        return "".join(parts)

    def _render_item(self, item: dict[str, Any]) -> str:
        conv = item.get("conversation") or {}
        conv_id = item["conversation_id"]
        e = html.escape
        attendees = ", ".join(
            f"{e(a.get('name', ''))}" + (f" &lt;{e(a['email'])}&gt;" if a.get("email") else " (no email)")
            for a in conv.get("attendees", [])
        )
        proposed: RecordRef | None = item["proposed_record"]
        options = ([proposed] if proposed else []) + list(item["alternatives"])
        record_html = []
        for idx, rec in enumerate(options):
            checked = " checked" if idx == 0 else ""
            label = f"{e(rec.entity)} #{rec.id} — {e(rec.name)}" + (f" &lt;{e(rec.email)}&gt;" if rec.email else " (no email)") + (f" — {e(rec.detail)}" if rec.detail else "")
            record_html.append(f"<label class='opt'><input type='radio' name='record' value='{e(rec.ref)}'{checked}> {label}</label>")
        custom_checked = "" if options else " checked"
        record_html.append(
            f"<label class='opt'><input type='radio' name='record' value='custom'{custom_checked}> Reassign to "
            "<select name='custom_entity'><option value='Candidate'>Candidate</option><option value='ClientContact'>ClientContact</option></select>"
            " id <input name='custom_id' size='10' placeholder='Bullhorn id'></label>"
        )
        job_options = "".join(
            f"<option value='{e(str(jo['id']))}'>#{e(str(jo['id']))} {e(jo.get('title') or '')}"
            + (f" ({e(jo['company'])})" if jo.get("company") else "") + "</option>"
            for jo in item.get("job_order_options", [])
        )
        job_html = (
            f"<input name='job_order_id' size='10' list='jo-{e(conv_id)}' placeholder='optional'>"
            + (f"<datalist id='jo-{e(conv_id)}'>{job_options}</datalist>" if job_options else "")
        )
        confidence = item.get("confidence") or "none"
        status = item["status"]
        error = item.get("error")
        source_label = "Metaview notes" if conv.get("body_source") == "notes" else "transcript"
        return (
            f"<section class='item {e(confidence)}'>"
            f"<h2>{e(conv.get('title') or conv_id)} <span class='meta'>{e(conv.get('occurred_at') or '')}</span></h2>"
            f"<p class='meta'>Attendees: {attendees} · <a href='{e(conv.get('url') or '#')}' target='_blank'>open in Metaview</a>"
            f" · status: <b>{e(status)}</b> · confidence: <b>{e(confidence)}</b></p>"
            f"<p class='reason'>{e(item.get('match_reason') or '')}</p>"
            + (f"<p class='flash err'>Last error: {e(error)}</p>" if error else "")
            + f"<form method='post' action='/approve'><input type='hidden' name='id' value='{e(conv_id)}'>"
            f"<fieldset><legend>Bullhorn record</legend>{''.join(record_html)}</fieldset>"
            f"<p><label>Action type <input name='action' value='{e(item.get('action_type') or '')}'></label> "
            f"<label>Job order {job_html}</label></p>"
            f"<p><label>Note (2–4 sentences, what was discussed and the next step; company-visible)<br>"
            f"<textarea name='note' rows='5' cols='100'>{e(item.get('draft_note') or '')}</textarea></label></p>"
            f"<details><summary>Source material ({e(source_label)}, local only, never written to Bullhorn)</summary>"
            f"<pre>{e(conv.get('body') or '')}</pre></details>"
            f"<p><button class='approve'>Approve</button></p></form>"
            f"<form method='post' action='/reject' class='inline'><input type='hidden' name='id' value='{e(conv_id)}'>"
            f"<button class='reject'>Reject</button></form>"
            "</section>"
        )


CSS = """
body{font:15px/1.4 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin:.2rem 0}
.item{border:1px solid #ccc;border-left:6px solid #999;border-radius:6px;padding:1rem;margin:1rem 0}
.item.high{border-left-color:#2a7}.item.low{border-left-color:#e90}.item.none{border-left-color:#c33}
.meta{color:#666;font-weight:normal;font-size:.9rem}.reason{background:#f6f6f6;padding:.5rem;border-radius:4px}
.opt{display:block;margin:.2rem 0}fieldset{border:1px solid #ddd;border-radius:4px}
textarea{width:100%;font:inherit}pre{white-space:pre-wrap;background:#fafafa;padding:.5rem;max-height:20rem;overflow:auto}
button{font:inherit;padding:.4rem 1rem;border-radius:4px;border:1px solid #888;cursor:pointer}
.approve{background:#2a7;color:#fff;border-color:#2a7}.reject{background:#fff;color:#c33;border-color:#c33}
.flash{padding:.5rem;border-radius:4px}.ok{background:#e6f7ee}.err{background:#fde8e8}
.inline{display:inline}.counts{color:#555}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:.2rem .5rem;font-size:.85rem}
"""


def make_handler(app: ConfirmationApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # route to logging
            log.debug("http %s", fmt % args)

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, message: str | None = None, error: str | None = None) -> None:
            query = []
            if message:
                query.append("msg=" + quote(message))
            if error:
                query.append("err=" + quote(error))
            self.send_response(303)
            self.send_header("Location", "/" + ("?" + "&".join(query) if query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in ("/", "/index.html"):
                self._send(404, "<p>not found</p>")
                return
            query = parse_qs(parsed.query)
            self._send(200, app.render_index(message=(query.get("msg") or [None])[0], error=(query.get("err") or [None])[0]))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            actions = {"/approve": app.approve, "/reject": app.reject, "/reopen": app.reopen}
            action = actions.get(parsed.path)
            if action is None:
                self._send(404, "<p>not found</p>")
                return
            try:
                self._redirect(message=action(form))
            except (SyncError, KeyError, ValueError) as exc:
                log.warning("%s rejected: %s", parsed.path, exc)
                self._redirect(error=str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("%s failed", parsed.path)
                self._redirect(error=f"unexpected error: {exc}")

    return Handler


def create_server(app: ConfirmationApp, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host or app.settings.confirm_host
    port = app.settings.confirm_port if port is None else port
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def serve_in_thread(app: ConfirmationApp) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = create_server(app)
    thread = threading.Thread(target=server.serve_forever, name="confirm-web", daemon=True)
    thread.start()
    log.info("confirmation page at http://%s:%d/", *server.server_address[:2])
    return server, thread
