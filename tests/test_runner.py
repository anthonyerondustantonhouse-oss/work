import unittest
from unittest import mock

from metaview_bullhorn.errors import ExtractionError, WriteRefused
from metaview_bullhorn.models import CONFIDENCE_HIGH, MatchResult
from metaview_bullhorn.runner import run_once, write_confirmed, write_item
from metaview_bullhorn.store import STATUS_CONFIRMED, STATUS_FAILED, STATUS_PENDING, STATUS_WRITTEN, Store
from tests.helpers import FakeBullhorn, FakeGenerator, FakeReader, make_conversation, make_settings, record

GOOD = "Discussed the role and timing. Next call booked for Friday."


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.store = Store(":memory:")
        self.bh = FakeBullhorn(by_email={"jane@example.com": [record()]})
        self.gen = FakeGenerator(GOOD)

    def run(self, *a, **k):  # unittest's own run() must be preserved
        return super().run(*a, **k)

    def _run(self, conversations=None, error=None, settings=None, gen=None, dry_run=False):
        reader = FakeReader(conversations or [], error=error)
        return run_once(settings or self.settings, self.store, self.bh, gen or self.gen, lambda: reader, dry_run=dry_run)

    def test_new_high_match_queues_when_confirmation_required(self):
        stats = self._run([make_conversation()])
        self.assertTrue(stats.ok)
        self.assertEqual((stats.found, stats.new, stats.matched, stats.queued, stats.written, stats.errors), (1, 1, 1, 1, 0, 0))
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_PENDING)
        self.assertEqual(item["confidence"], CONFIDENCE_HIGH)
        self.assertEqual(item["draft_note"], GOOD)
        self.assertEqual(item["action_type"], "Candidate Call")
        self.assertEqual(self.bh.notes, [])
        self.assertEqual(stats.decisions[0]["status"], STATUS_PENDING)
        self.assertEqual(self.store.recent_runs(1)[0]["ok"], 1)

    def test_dedup_second_run_skips(self):
        self._run([make_conversation()])
        stats = self._run([make_conversation()])
        self.assertEqual((stats.found, stats.new, stats.queued), (1, 0, 0))
        self.assertEqual(self.gen.calls, 1)

    def test_auto_confirm_only_for_high_when_allowed(self):
        settings = make_settings(REQUIRE_CONFIRMATION_FOR_ALL="false")
        low_conv = make_conversation(conv_id="low", attendees=[make_conversation().attendees[1].__class__("Bob Low", "bob@example.com")])
        self.bh.by_name["bob low"] = [record(record_id=300, name="Bob Low", email=None)]
        stats = self._run([make_conversation(), low_conv], settings=settings)
        self.assertTrue(stats.ok)
        self.assertEqual(self.store.status_of("abc123"), STATUS_WRITTEN)
        self.assertEqual(self.store.status_of("low"), STATUS_PENDING)
        self.assertEqual(stats.written, 1)
        self.assertEqual(self.bh.notes[0]["record"], "Candidate:101")
        self.assertEqual(self.bh.notes[0]["comments"], GOOD)
        self.assertEqual(self.bh.notes[0]["action"], "Candidate Call")

    def test_extraction_error_aborts_without_writing(self):
        self.store.add_new(make_conversation(conv_id="ready"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_CONFIRMED)
        stats = self._run(error=ExtractionError("title", "selector changed", "c9"))
        self.assertFalse(stats.ok)
        self.assertIn("ExtractionError", stats.error_message)
        self.assertIn("title", stats.error_message)
        self.assertEqual(self.bh.notes, [])
        self.assertEqual(self.store.status_of("ready"), STATUS_CONFIRMED)
        self.assertEqual(self.store.consecutive_failures(), 1)

    def test_confirmed_items_written_on_run(self):
        self.store.add_new(make_conversation(conv_id="ready"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_CONFIRMED)
        stats = self._run([])
        self.assertTrue(stats.ok)
        self.assertEqual(stats.written, 1)
        item = self.store.get("ready")
        self.assertEqual(item["status"], STATUS_WRITTEN)
        self.assertEqual(item["note_id"], 5001)

    def test_write_failure_marks_failed_and_aborts(self):
        self.store.add_new(make_conversation(conv_id="a"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_CONFIRMED)
        self.store.add_new(make_conversation(conv_id="b"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_CONFIRMED)
        self.bh.fail_writes = True
        stats = self._run([])
        self.assertFalse(stats.ok)
        self.assertEqual(self.store.status_of("a"), STATUS_FAILED)
        self.assertEqual(self.store.status_of("b"), STATUS_CONFIRMED)
        self.assertIn("simulated write failure", self.store.get("a")["error"])

    def test_note_generation_failure_still_queues(self):
        stats = self._run([make_conversation()], gen=FakeGenerator(error="no key"))
        self.assertFalse(stats.ok)
        self.assertEqual(stats.errors, 1)
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_PENDING)
        self.assertEqual(item["draft_note"], "")
        self.assertIn("no key", item["error"])

    def test_bad_draft_never_auto_confirms(self):
        settings = make_settings(REQUIRE_CONFIRMATION_FOR_ALL="false")
        self._run([make_conversation()], settings=settings, gen=FakeGenerator(error="down"))
        self.assertEqual(self.store.status_of("abc123"), STATUS_PENDING)
        self.assertEqual(self.bh.notes, [])

    def test_dry_run_changes_nothing(self):
        stats = self._run([make_conversation()], dry_run=True)
        self.assertTrue(stats.ok)
        self.assertEqual(stats.decisions[0]["proposed"], "Candidate:101")
        self.assertFalse(self.store.is_known("abc123"))
        self.assertEqual(self.store.recent_runs(), [])

    def test_alert_after_three_failures(self):
        with mock.patch("metaview_bullhorn.runner.send_alert") as alert:
            for _ in range(2):
                self._run(error=RuntimeError("boom"))
            alert.assert_not_called()
            self._run(error=RuntimeError("boom"))
            alert.assert_called_once()
            self.assertIn("3 consecutive", alert.call_args[0][1])
            self._run([])
            self.assertEqual(self.store.consecutive_failures(), 0)

    def test_write_item_guards(self):
        self.store.add_new(make_conversation(conv_id="p"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_PENDING)
        with self.assertRaises(WriteRefused):
            write_item(self.store, self.bh, self.store.get("p"))
        self.store.confirm("p", record(), "Too short.", "Call", None)
        item = self.store.get("p")
        item["draft_note"] = "One sentence."
        from metaview_bullhorn.errors import NoteValidationError

        with self.assertRaises(NoteValidationError):
            write_item(self.store, self.bh, item)
        self.assertEqual(self.bh.notes, [])
        item = self.store.get("p")
        item["note_id"] = 1
        with self.assertRaises(WriteRefused):
            write_item(self.store, self.bh, item)

    def test_write_confirmed_helper(self):
        self.store.add_new(make_conversation(conv_id="p"), MatchResult(CONFIDENCE_HIGH, "r", record()), GOOD, "Call", STATUS_CONFIRMED)
        self.assertEqual(write_confirmed(self.store, self.bh), 1)
        self.assertEqual(write_confirmed(self.store, self.bh), 0)
        self.assertEqual(len(self.bh.notes), 1)


if __name__ == "__main__":
    unittest.main()
