import tempfile
import unittest
from pathlib import Path

from metaview_bullhorn.config import load_env_file, load_settings, parse_record_ref
from metaview_bullhorn.errors import ConfigError
from tests.helpers import BASE_ENV, make_settings


class ConfigTests(unittest.TestCase):
    def test_missing_required_lists_every_name(self):
        env = dict(BASE_ENV)
        del env["BULLHORN_CLIENT_ID"]
        env["METAVIEW_BASE_URL"] = "  "
        with self.assertRaises(ConfigError) as ctx:
            load_settings(environ=env)
        self.assertIn("BULLHORN_CLIENT_ID", str(ctx.exception))
        self.assertIn("METAVIEW_BASE_URL", str(ctx.exception))

    def test_defaults(self):
        s = make_settings()
        self.assertTrue(s.require_confirmation_for_all)
        self.assertFalse(s.write_on_approve)
        self.assertEqual(s.sync_interval_minutes, 30)
        self.assertEqual(s.consecutive_failure_alert_threshold, 3)
        self.assertEqual(s.note_model, "claude-opus-5")
        self.assertEqual(s.metaview_conversations_url, "https://app.metaview.test/conversations")
        self.assertIn("ourfirm.com", s.internal_email_domains)
        self.assertIn("anthony erondu", s.internal_names)
        self.assertFalse(s.email_alerts_enabled)

    def test_action_map_override_and_validation(self):
        s = make_settings(NOTE_ACTION_MAP='{"Debrief": "Client Debrief"}')
        self.assertEqual(s.note_action_map, {"debrief": "Client Debrief"})
        with self.assertRaises(ConfigError):
            make_settings(NOTE_ACTION_MAP="not json")
        with self.assertRaises(ConfigError):
            make_settings(NOTE_ACTION_MAP='{"a": 1}')

    def test_bad_int(self):
        with self.assertRaises(ConfigError):
            make_settings(CONFIRM_PORT="abc")

    def test_test_record_validated_early(self):
        with self.assertRaises(ConfigError):
            make_settings(BULLHORN_TEST_RECORD="Lead:5")
        self.assertEqual(make_settings(BULLHORN_TEST_RECORD="Candidate:5").bullhorn_test_record, "Candidate:5")

    def test_parse_record_ref(self):
        self.assertEqual(parse_record_ref("ClientContact:42"), ("ClientContact", 42))
        for bad in ("Candidate:", "Candidate:x", "Candidate:-1", "Foo:1"):
            with self.assertRaises(ConfigError):
                parse_record_ref(bad)

    def test_env_file_fallback_parser(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nMV_TEST_A=one\nMV_TEST_B="two words"\nMV_TEST_C=\'x\'\n\nJUNK\n')
            os.environ.pop("MV_TEST_A", None)
            os.environ["MV_TEST_B"] = "preset"
            try:
                load_env_file(path)
                self.assertEqual(os.environ["MV_TEST_A"], "one")
                self.assertEqual(os.environ["MV_TEST_B"], "preset")  # never overrides
                self.assertEqual(os.environ["MV_TEST_C"], "x")
            finally:
                for k in ("MV_TEST_A", "MV_TEST_B", "MV_TEST_C"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
