import unittest

from metaview_bullhorn.models import CONFIDENCE_LOW, MatchResult
from metaview_bullhorn.store import STATUS_CONFIRMED, STATUS_FAILED, STATUS_PENDING, STATUS_SKIPPED, STATUS_WRITTEN, Store
from tests.helpers import make_conversation, record


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.conv = make_conversation()
        self.match = MatchResult(CONFIDENCE_LOW, "name only", record(), alternatives=[record(record_id=102)], job_orders=[{"id": 1, "title": "x"}])

    def test_add_and_get(self):
        self.assertFalse(self.store.is_known("abc123"))
        self.store.add_new(self.conv, self.match, "draft", "Call", STATUS_PENDING)
        self.assertTrue(self.store.is_known("abc123"))
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_PENDING)
        self.assertEqual(item["proposed_record"].id, 101)
        self.assertEqual(item["alternatives"][0].id, 102)
        self.assertEqual(item["job_order_options"][0]["id"], 1)
        self.assertEqual(item["conversation"]["title"], self.conv.title)
        self.assertEqual(item["bullhorn_record_id"], 101)

    def test_duplicate_insert_refused(self):
        self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)
        with self.assertRaises(Exception):
            self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)

    def test_cannot_start_written_or_confirm_without_record(self):
        with self.assertRaises(ValueError):
            self.store.add_new(self.conv, self.match, "d", "Call", STATUS_WRITTEN)
        with self.assertRaises(ValueError):
            self.store.add_new(self.conv, MatchResult("none", "nothing", None), "d", "Call", STATUS_CONFIRMED)

    def test_lifecycle_pending_confirm_written(self):
        self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)
        self.store.confirm("abc123", record(record_id=102), "Final note here. Next call Friday.", "Candidate Call", 77)
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_CONFIRMED)
        self.assertEqual(item["bullhorn_record_id"], 102)
        self.assertEqual(item["job_order_id"], 77)
        self.assertEqual([i["conversation_id"] for i in self.store.confirmed()], ["abc123"])
        self.store.mark_written("abc123", 9001)
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_WRITTEN)
        self.assertEqual(item["note_id"], 9001)
        # Written is terminal: nothing can move it.
        with self.assertRaises(ValueError):
            self.store.mark_written("abc123", 9002)
        with self.assertRaises(ValueError):
            self.store.confirm("abc123", record(), "x. y.", "Call", None)
        with self.assertRaises(ValueError):
            self.store.skip("abc123")
        with self.assertRaises(ValueError):
            self.store.mark_failed("abc123", "boom")

    def test_only_confirmed_can_be_written(self):
        self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)
        with self.assertRaises(ValueError):
            self.store.mark_written("abc123", 1)

    def test_skip_reopen_fail(self):
        self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)
        self.store.skip("abc123", "no")
        self.assertEqual(self.store.status_of("abc123"), STATUS_SKIPPED)
        self.assertEqual(self.store.queue(), [])
        self.store.reopen("abc123")
        self.assertEqual(self.store.status_of("abc123"), STATUS_PENDING)
        self.store.mark_failed("abc123", "x" * 5000)
        self.assertEqual(self.store.status_of("abc123"), STATUS_FAILED)
        self.assertEqual(len(self.store.queue()), 1)
        # failed items can be confirmed again from the page
        self.store.confirm("abc123", record(), "a. b.", "Call", None)
        self.assertEqual(self.store.status_of("abc123"), STATUS_CONFIRMED)

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            self.store.skip("nope")

    def test_counts_and_runs(self):
        self.store.add_new(self.conv, self.match, "d", "Call", STATUS_PENDING)
        self.assertEqual(self.store.counts()[STATUS_PENDING], 1)
        r1 = self.store.start_run()
        self.store.finish_run(r1, True, {"found": 3})
        r2 = self.store.start_run()
        self.store.finish_run(r2, False, {}, "boom")
        r3 = self.store.start_run()
        self.store.finish_run(r3, False, {}, "boom")
        self.assertEqual(self.store.consecutive_failures(), 2)
        r4 = self.store.start_run()  # unfinished runs do not count
        self.assertEqual(self.store.consecutive_failures(), 2)
        self.store.finish_run(r4, True, {})
        self.assertEqual(self.store.consecutive_failures(), 0)
        self.assertEqual(self.store.recent_runs(1)[0]["id"], r4)


if __name__ == "__main__":
    unittest.main()
