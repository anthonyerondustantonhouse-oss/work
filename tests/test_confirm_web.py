import unittest
import urllib.error
import urllib.parse
import urllib.request

from metaview_bullhorn.confirm_web import ConfirmationApp, create_server
from metaview_bullhorn.errors import SyncError
from metaview_bullhorn.models import CONFIDENCE_LOW, CONFIDENCE_NONE, MatchResult
from metaview_bullhorn.store import STATUS_CONFIRMED, STATUS_PENDING, STATUS_SKIPPED, Store
from tests.helpers import FakeBullhorn, make_conversation, make_settings, record

GOOD = "Discussed the role and timing. Next call booked for Friday."


class ConfirmationAppTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.store = Store(":memory:")
        self.bh = FakeBullhorn()
        self.bh.records["ClientContact:555"] = record("ClientContact", 555, "Sam Client", "sam@acme.com")
        self.written = []
        self.app = ConfirmationApp(self.settings, self.store, record_lookup=self.bh.get_record, writer=self._writer)
        self.store.add_new(
            make_conversation(),
            MatchResult(CONFIDENCE_LOW, "name only", record(), alternatives=[record(record_id=102, name="Jane Doe (2)")]),
            "draft <script>",
            "Candidate Call",
            STATUS_PENDING,
        )

    def _writer(self, conv_id):
        self.written.append(conv_id)
        return 999

    def test_render_escapes_and_shows_items(self):
        html = self.app.render_index(message="hi", error="<b>")
        self.assertIn("draft &lt;script&gt;", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("Candidate #101", html)
        self.assertIn("Candidate #102", html)
        self.assertIn("Reassign", html)
        self.assertIn("name only", html)

    def test_approve_proposed(self):
        msg = self.app.approve({"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Candidate Call", "job_order_id": "77"})
        self.assertIn("next run", msg)
        item = self.store.get("abc123")
        self.assertEqual(item["status"], STATUS_CONFIRMED)
        self.assertEqual(item["job_order_id"], 77)
        self.assertEqual(item["draft_note"], GOOD)
        self.assertEqual(self.written, [])

    def test_approve_alternative(self):
        self.app.approve({"id": "abc123", "record": "Candidate:102", "note": GOOD, "action": "Call"})
        self.assertEqual(self.store.get("abc123")["bullhorn_record_id"], 102)

    def test_approve_reassign_validates_against_bullhorn(self):
        with self.assertRaises(SyncError):
            self.app.approve({"id": "abc123", "record": "custom", "custom_entity": "Candidate", "custom_id": "1", "note": GOOD, "action": "Call"})
        self.app.approve({"id": "abc123", "record": "custom", "custom_entity": "ClientContact", "custom_id": "555", "note": GOOD, "action": "Call"})
        item = self.store.get("abc123")
        self.assertEqual((item["bullhorn_entity"], item["bullhorn_record_id"]), ("ClientContact", 555))
        self.assertEqual(item["proposed_record"].name, "Sam Client")

    def test_approve_rejects_bad_note_action_and_job(self):
        for form in (
            {"id": "abc123", "record": "Candidate:101", "note": "One sentence only.", "action": "Call"},
            {"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": ""},
            {"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Call", "job_order_id": "x"},
            {"id": "abc123", "record": "Candidate:999", "note": GOOD, "action": "Call"},
            {"id": "nope", "record": "Candidate:101", "note": GOOD, "action": "Call"},
        ):
            with self.assertRaises(SyncError, msg=str(form)):
                self.app.approve(form)
        self.assertEqual(self.store.get("abc123")["status"], STATUS_PENDING)

    def test_approve_without_record_needs_reassign(self):
        self.store.add_new(make_conversation(conv_id="z"), MatchResult(CONFIDENCE_NONE, "nothing", None), "", "Call", STATUS_PENDING)
        with self.assertRaises(SyncError):
            self.app.approve({"id": "z", "record": "proposed", "note": GOOD, "action": "Call"})

    def test_write_on_approve(self):
        app = ConfirmationApp(make_settings(WRITE_ON_APPROVE="true"), self.store, record_lookup=self.bh.get_record, writer=self._writer)
        msg = app.approve({"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Call"})
        self.assertIn("note 999", msg)
        self.assertEqual(self.written, ["abc123"])

    def test_reject_and_reopen(self):
        self.app.reject({"id": "abc123"})
        self.assertEqual(self.store.status_of("abc123"), STATUS_SKIPPED)
        self.app.reopen({"id": "abc123"})
        self.assertEqual(self.store.status_of("abc123"), STATUS_PENDING)

    def test_cannot_approve_twice(self):
        self.app.approve({"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Call"})
        with self.assertRaises(SyncError):
            self.app.approve({"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Call"})


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.add_new(make_conversation(), MatchResult(CONFIDENCE_LOW, "name only", record()), "", "Call", STATUS_PENDING)
        self.app = ConfirmationApp(make_settings(), self.store)
        self.server = create_server(self.app, host="127.0.0.1", port=0)
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, data):
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(data).encode(), method="POST")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(req)
            return resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Location", "")

    def test_get_index(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("confirmation queue", body)
        self.assertIn("Candidate screen - Jane Doe", body)

    def test_post_approve_and_reject(self):
        status, location = self._post("/approve", {"id": "abc123", "record": "Candidate:101", "note": "Bad.", "action": "Call"})
        self.assertEqual(status, 303)
        self.assertIn("err=", location)
        self.assertEqual(self.store.status_of("abc123"), STATUS_PENDING)
        status, location = self._post("/approve", {"id": "abc123", "record": "Candidate:101", "note": GOOD, "action": "Call"})
        self.assertEqual(status, 303)
        self.assertIn("msg=", location)
        self.assertEqual(self.store.status_of("abc123"), STATUS_CONFIRMED)

    def test_unknown_routes(self):
        self.assertEqual(self._post("/nope", {})[0], 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
