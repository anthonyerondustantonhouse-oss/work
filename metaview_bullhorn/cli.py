"""Command line entry point. Subcommands follow the build order:

  login            open a headed browser on Metaview so you can sign in once (profile persists)
  probe            save the conversations page HTML + screenshot for selector tuning
  read             step 1: read the conversation list and print it
  bullhorn-check   step 3: authenticate to Bullhorn and read one record
  match            step 4: read Metaview and print the proposed match per conversation (no state changes)
  run              one full run (steps 2-6); --dry-run to change nothing
  serve            step 5: the confirmation web page
  write-confirmed  write approved items now instead of waiting for the next run
  test-note        step 6: write one note to the throwaway BULLHORN_TEST_RECORD
  schedule         step 7: run every SYNC_INTERVAL_MINUTES, serving the web page alongside
  status           queue counts and recent runs
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .alerts import send_alert
from .bullhorn import BullhornClient
from .config import Settings, load_settings, parse_record_ref
from .confirm_web import ConfirmationApp, create_server, serve_in_thread
from .errors import ConfigError, SyncError
from .matcher import match_conversation
from .metaview_reader import MetaviewReader
from .note_writer import NoteGenerator, infer_action_type, validate_note
from .runner import run_once, write_confirmed, write_item
from .store import STATUS_CONFIRMED, Store

log = logging.getLogger("metaview_bullhorn")


def setup_logging(settings: Settings, verbose: bool) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler = logging.FileHandler(settings.log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers[:] = [file_handler, stream]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mvsync", description="Metaview to Bullhorn note sync")
    parser.add_argument("--env", default=".env", help="path to the .env file (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="open a visible browser to sign in to Metaview once")
    p = sub.add_parser("probe", help="save page HTML, screenshot and links for selector tuning")
    p.add_argument("--url", help="page to probe (default: the conversations list)")
    p.add_argument("--out", default="./probe", help="output directory")
    p = sub.add_parser("read", help="read the conversation list and print it")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("bullhorn-check", help="authenticate and read one record")
    p.add_argument("record", nargs="?", help="Candidate:123 or ClientContact:123 (default: BULLHORN_TEST_RECORD)")
    p = sub.add_parser("match", help="print proposed matches without changing state")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("run", help="one full sync run")
    p.add_argument("--dry-run", action="store_true", help="read and match but change nothing")
    p = sub.add_parser("serve", help="confirmation web page")
    p.add_argument("--port", type=int)
    sub.add_parser("write-confirmed", help="write approved items to Bullhorn now")
    p = sub.add_parser("test-note", help="write one test note to BULLHORN_TEST_RECORD")
    p.add_argument("--note", help="note text (default: a fixed two-sentence test note)")
    p.add_argument("--action", default="Note")
    p = sub.add_parser("schedule", help="run on the configured interval with the web page alongside")
    p.add_argument("--no-web", action="store_true", help="do not serve the confirmation page")
    p.add_argument("--no-initial-run", action="store_true")
    sub.add_parser("status", help="queue counts and recent runs")
    sub.add_parser("test-alert", help="send a test alert on every configured channel")
    return parser


# ---- commands ------------------------------------------------------------


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    with MetaviewReader(settings, headless=False) as reader:
        reader.goto(settings.metaview_conversations_url)
        print("A browser window is open. Sign in to Metaview, wait for the conversations list, then press Enter here.")
        input()
        reader.goto(settings.metaview_conversations_url)
        if reader.is_logged_in():
            print(f"Logged in. Profile saved to {settings.playwright_profile_dir}")
            return 0
        print("Still looks logged out (login form or auth URL detected). Try again.")
        return 1


def cmd_probe(settings: Settings, args: argparse.Namespace) -> int:
    with MetaviewReader(settings) as reader:
        out = reader.probe(Path(args.out), url=args.url)
    print(f"Saved page.html, page.png and links.txt to {out}. Adjust selectors in metaview_bullhorn/selectors.py or METAVIEW_SELECTORS_FILE.")
    return 0


def cmd_read(settings: Settings, args: argparse.Namespace) -> int:
    with MetaviewReader(settings) as reader:
        conversations = reader.read_all(limit=args.limit)
    if args.json:
        print(json.dumps([c.to_dict() for c in conversations], indent=2, ensure_ascii=False))
        return 0
    for c in conversations:
        attendees = ", ".join(f"{a.name}" + (f" <{a.email}>" if a.email else "") for a in c.attendees)
        print(f"{c.id}  {c.occurred_at}  {c.title}\n    attendees: {attendees}\n    body: {len(c.body)} chars from {c.body_source}\n    {c.url}")
    print(f"{len(conversations)} conversation(s)")
    return 0


def cmd_bullhorn_check(settings: Settings, args: argparse.Namespace) -> int:
    ref = args.record or settings.bullhorn_test_record
    if not ref:
        print("Give a record like Candidate:123, or set BULLHORN_TEST_RECORD.", file=sys.stderr)
        return 2
    entity, record_id = parse_record_ref(ref)
    bh = BullhornClient(settings)
    bh.login()
    record = bh.get_record(entity, record_id)
    print(f"Auth ok (restUrl={bh.rest_url})\n{record.entity} #{record.id}: {record.name} <{record.email}> {record.detail or ''}")
    return 0


def cmd_match(settings: Settings, args: argparse.Namespace) -> int:
    with MetaviewReader(settings) as reader:
        conversations = reader.read_all(limit=args.limit)
    bh = BullhornClient(settings)
    store = Store(settings.db_path)
    for c in conversations:
        status = store.status_of(c.id)
        result = match_conversation(c, bh, settings)
        proposed = f"{result.proposed.ref} {result.proposed.name} <{result.proposed.email}>" if result.proposed else "-"
        print(f"{c.id}  {c.title}\n    tracked: {status or 'new'}  action: {infer_action_type(c, settings)}\n    {result.confidence.upper()}: {proposed}\n    {result.reason}")
        for alt in result.alternatives:
            print(f"    alt: {alt.ref} {alt.name} <{alt.email}>")
    return 0


def _components(settings: Settings):
    store = Store(settings.db_path)
    bh = BullhornClient(settings)
    generator = NoteGenerator(settings)
    return store, bh, generator


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    store, bh, generator = _components(settings)
    stats = run_once(settings, store, bh, generator, lambda: MetaviewReader(settings), dry_run=args.dry_run)
    for d in stats.decisions:
        print(f"{d['conversation_id']}  {d['title']}\n    {d['confidence']} -> {d['proposed']}  status={d['status']}  action={d['action_type']}")
        if d["error"]:
            print(f"    error: {d['error']}")
        if d["draft"]:
            print(f"    draft: {d['draft']}")
    print(stats.summary())
    return 0 if stats.ok else 1


def _app(settings: Settings, store: Store) -> ConfirmationApp:
    bh = BullhornClient(settings)

    def writer(conv_id: str) -> int:
        item = store.get(conv_id)
        assert item is not None
        return write_item(store, bh, item)

    return ConfirmationApp(settings, store, record_lookup=bh.get_record, writer=writer)


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    server = create_server(_app(settings, store), port=args.port)
    host, port = server.server_address[:2]
    print(f"Confirmation page: http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_write_confirmed(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    pending = store.confirmed()
    if not pending:
        print("Nothing confirmed.")
        return 0
    bh = BullhornClient(settings)
    try:
        n = write_confirmed(store, bh)
    except SyncError as exc:
        print(f"Write failed: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {n} note(s).")
    return 0


def cmd_test_note(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.bullhorn_test_record:
        print("Set BULLHORN_TEST_RECORD to a throwaway record first.", file=sys.stderr)
        return 2
    entity, record_id = parse_record_ref(settings.bullhorn_test_record)
    note = validate_note(args.note or "Test note from the Metaview sync build. This can be deleted.")
    bh = BullhornClient(settings)
    bh.login()
    record = bh.get_record(entity, record_id)
    print(f"About to write to {record.entity} #{record.id} ({record.name}). Type the record id to confirm: ", end="")
    if input().strip() != str(record.id):
        print("Aborted.")
        return 1
    note_id = bh.create_note(record, note, args.action)
    stored = bh.get_note(note_id)
    print(f"Wrote note {note_id}: action={stored.get('action')} comments={stored.get('comments')!r}")
    return 0


def cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    from .scheduler import run_forever

    store, bh, generator = _components(settings)
    if not args.no_web:
        serve_in_thread(_app(settings, store))
        print(f"Confirmation page: http://{settings.confirm_host}:{settings.confirm_port}/")

    def job() -> None:
        stats = run_once(settings, store, bh, generator, lambda: MetaviewReader(settings))
        print(stats.summary())

    run_forever(settings, job, run_immediately=not args.no_initial_run)
    return 0


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    store = Store(settings.db_path)
    print("processed:", ", ".join(f"{k}={v}" for k, v in store.counts().items()))
    print(f"consecutive failed runs: {store.consecutive_failures()}")
    for run in store.recent_runs(10):
        print(
            f"  {run['started_at']} ok={run['ok']} found={run['found']} new={run['new']} matched={run['matched']} "
            f"queued={run['queued']} written={run['written']} errors={run['errors']}"
            + (f" error={run['error_message']}" if run["error_message"] else "")
        )
    confirmed = store.confirmed()
    if confirmed:
        print(f"{len(confirmed)} item(s) confirmed and waiting to be written ({STATUS_CONFIRMED}).")
    return 0


def cmd_test_alert(settings: Settings, args: argparse.Namespace) -> int:
    delivered = send_alert(settings, "Metaview→Bullhorn sync: test alert", "If you can read this, alerts work.")
    print("delivered via: " + (", ".join(delivered) or "nothing (check ALERT_* settings)"))
    return 0 if delivered else 1


COMMANDS = {
    "login": cmd_login,
    "probe": cmd_probe,
    "read": cmd_read,
    "bullhorn-check": cmd_bullhorn_check,
    "match": cmd_match,
    "run": cmd_run,
    "serve": cmd_serve,
    "write-confirmed": cmd_write_confirmed,
    "test-note": cmd_test_note,
    "schedule": cmd_schedule,
    "status": cmd_status,
    "test-alert": cmd_test_alert,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings, args.verbose)
    try:
        return COMMANDS[args.command](settings, args)
    except SyncError as exc:
        log.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
