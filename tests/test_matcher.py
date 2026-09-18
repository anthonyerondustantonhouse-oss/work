import unittest

from metaview_bullhorn.matcher import external_attendees, is_internal, match_conversation
from metaview_bullhorn.models import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_NONE, Attendee
from tests.helpers import FakeBullhorn, make_conversation, make_settings, record


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(INTERNAL_EMAILS="ext.contractor@gmail.com")

    def test_internal_detection(self):
        self.assertTrue(is_internal(Attendee("Someone", "bob@OurFirm.com"), self.settings))
        self.assertTrue(is_internal(Attendee("anthony erondu", None), self.settings))
        self.assertTrue(is_internal(Attendee("X", "ext.contractor@gmail.com"), self.settings))
        self.assertFalse(is_internal(Attendee("Jane Doe", "jane@example.com"), self.settings))
        conv = make_conversation()
        self.assertEqual([a.name for a in external_attendees(conv, self.settings)], ["Jane Doe"])

    def test_single_email_hit_is_high(self):
        bh = FakeBullhorn(by_email={"jane@example.com": [record()]})
        result = match_conversation(make_conversation(), bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)
        self.assertEqual(result.proposed.id, 101)
        self.assertEqual(result.alternatives, [])
        self.assertEqual(result.job_orders[0]["id"], 77)

    def test_multiple_email_hits_is_low(self):
        bh = FakeBullhorn(by_email={"jane@example.com": [record(), record("ClientContact", 202)]})
        result = match_conversation(make_conversation(), bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_LOW)
        self.assertEqual(result.proposed.id, 101)
        self.assertEqual([a.id for a in result.alternatives], [202])
        self.assertIn("2 Bullhorn records", result.reason)

    def test_name_fallback_is_low_even_with_single_hit(self):
        bh = FakeBullhorn(by_name={"jane doe": [record()]})
        result = match_conversation(make_conversation(), bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_LOW)
        self.assertEqual(result.proposed.id, 101)
        self.assertIn("Name search only", result.reason)
        self.assertIn("jane@example.com", result.reason)

    def test_missing_email_uses_name_and_is_low(self):
        conv = make_conversation(attendees=[Attendee("Anthony Erondu", None), Attendee("Jane Doe", None)])
        bh = FakeBullhorn(by_name={"jane doe": [record(), record(record_id=103)]})
        result = match_conversation(conv, bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_LOW)
        self.assertIn("exposed no email", result.reason)
        self.assertEqual(len(result.alternatives), 1)

    def test_no_match(self):
        result = match_conversation(make_conversation(), FakeBullhorn(), self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_NONE)
        self.assertIsNone(result.proposed)

    def test_all_internal(self):
        conv = make_conversation(attendees=[Attendee("Anthony Erondu", "anthony@ourfirm.com"), Attendee("Colleague", "c@ourfirm.com")])
        bh = FakeBullhorn(by_email={"c@ourfirm.com": [record()]})
        result = match_conversation(conv, bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_NONE)
        self.assertIn("No external attendee", result.reason)

    def test_two_externals_same_record_is_high(self):
        conv = make_conversation(attendees=[Attendee("Jane Doe", "jane@example.com"), Attendee("Jane D", "jane.doe@work.com")])
        bh = FakeBullhorn(by_email={"jane@example.com": [record()], "jane.doe@work.com": [record()]})
        result = match_conversation(conv, bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)

    def test_job_order_failure_is_not_fatal(self):
        bh = FakeBullhorn(by_email={"jane@example.com": [record()]})
        bh.job_order_suggestions = lambda rec, limit=10: (_ for _ in ()).throw(RuntimeError("boom"))
        result = match_conversation(make_conversation(), bh, self.settings)
        self.assertEqual(result.confidence, CONFIDENCE_HIGH)
        self.assertEqual(result.job_orders, [])


if __name__ == "__main__":
    unittest.main()
