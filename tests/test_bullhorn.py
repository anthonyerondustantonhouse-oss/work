import json
import unittest

from metaview_bullhorn.bullhorn import BullhornClient, lucene_quote, record_from_data
from metaview_bullhorn.errors import BullhornError
from tests.helpers import make_settings, record


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, url="", text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.url = url
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Routes requests to handlers keyed by (method, path-prefix)."""

    def __init__(self):
        self.calls = []
        self.token_counter = 0
        self.expire_first_rest_call = False
        self._expired_once = False
        self.search_results = {}
        self.note_response = {"changedEntityType": "Note", "changedEntityId": 4242, "changeType": "INSERT"}
        self.entity = {"data": {"id": 101, "firstName": "Jane", "lastName": "Doe", "name": "Jane Doe", "email": "Jane@Example.com", "status": "Active", "isDeleted": False}}

    def get(self, url, params=None, allow_redirects=True, timeout=None, **kw):
        return self.request("GET", url, params=params, allow_redirects=allow_redirects, timeout=timeout, **kw)

    def post(self, url, params=None, timeout=None, **kw):
        return self.request("POST", url, params=params, timeout=timeout, **kw)

    def request(self, method, url, params=None, headers=None, json=None, allow_redirects=True, timeout=None, **kw):
        self.calls.append((method, url, params, headers, json))
        if "oauth/authorize" in url:
            assert params["action"] == "Login"
            return FakeResponse(302, headers={"Location": "https://auth.example/intermediate"})
        if url == "https://auth.example/intermediate":
            assert params is None and allow_redirects is False
            return FakeResponse(302, headers={"Location": "https://redirect.example/?code=THECODE"})
        if "oauth/token" in url:
            assert params["code"] == "THECODE" and params["client_secret"] == "secret"
            self.token_counter += 1
            return FakeResponse(200, {"access_token": f"AT{self.token_counter}", "refresh_token": "rt"})
        if "rest-services/login" in url:
            assert params["access_token"].startswith("AT")
            return FakeResponse(200, {"BhRestToken": f"BH{self.token_counter}", "restUrl": "https://rest.example/rest-services/abc"})
        # REST calls
        assert headers and headers.get("BhRestToken"), "missing BhRestToken"
        if self.expire_first_rest_call and not self._expired_once:
            self._expired_once = True
            return FakeResponse(401, text="expired")
        path = url.split("/rest-services/abc/", 1)[1]
        if path.startswith("search/"):
            entity = path.split("/")[1]
            return FakeResponse(200, {"data": self.search_results.get(entity, []), "total": 0})
        if path.startswith("entity/Candidate/") or path.startswith("entity/ClientContact/"):
            return FakeResponse(200, self.entity)
        if path == "entity/Note" and method == "PUT":
            return FakeResponse(200, self.note_response)
        if path.startswith("entity/Note/"):
            return FakeResponse(200, {"data": {"id": 4242, "action": "Call", "comments": "x"}})
        if path.startswith("query/JobSubmission"):
            return FakeResponse(200, {"data": [{"id": 1, "status": "Submitted", "jobOrder": {"id": 77, "title": "Head of Sales", "clientCorporation": {"name": "Acme"}}}]})
        if path.startswith("query/JobOrder"):
            return FakeResponse(200, {"data": [{"id": 78, "title": "CFO", "clientCorporation": {"name": "Beta"}}]})
        return FakeResponse(404, text="nope")


class BullhornTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.client = BullhornClient(make_settings(), session=self.session)

    def test_login_flow(self):
        self.client.login()
        self.assertEqual(self.client.rest_token, "BH1")
        self.assertEqual(self.client.rest_url, "https://rest.example/rest-services/abc/")

    def test_get_record_normalises_email(self):
        rec = self.client.get_record("Candidate", 101)
        self.assertEqual(rec.email, "jane@example.com")
        self.assertEqual(rec.detail, "Active")
        self.session.entity = {"data": {"id": 101, "isDeleted": True}}
        with self.assertRaises(BullhornError):
            self.client.get_record("Candidate", 101)
        with self.assertRaises(BullhornError):
            self.client.get_record("Lead", 1)

    def test_relogin_on_401(self):
        self.session.expire_first_rest_call = True
        rec = self.client.get_record("Candidate", 101)
        self.assertEqual(rec.id, 101)
        self.assertEqual(self.client.rest_token, "BH2")

    def test_search_by_email_exact_only(self):
        self.session.search_results = {
            "Candidate": [
                {"id": 1, "name": "Jane Doe", "email": "JANE@example.com", "isDeleted": False},
                {"id": 2, "name": "Janet", "email": "jane@example.community", "isDeleted": False},  # analyser false positive
                {"id": 3, "name": "Deleted", "email": "jane@example.com", "isDeleted": True},
            ],
            "ClientContact": [{"id": 9, "name": "J", "email": "other@x.com", "email2": "jane@example.com", "clientCorporation": {"name": "Acme"}}],
        }
        hits = self.client.search_by_email("Jane@Example.com")
        self.assertEqual([(h.entity, h.id) for h in hits], [("Candidate", 1), ("ClientContact", 9)])
        self.assertEqual(hits[1].detail, "Acme")
        query = [c for c in self.session.calls if c[1].endswith("search/Candidate")][0][2]["query"]
        self.assertIn('email:"jane@example.com"', query)
        self.assertIn("isDeleted:false", query)
        self.assertEqual(self.client.search_by_email("  "), [])

    def test_search_by_name_query(self):
        self.session.search_results = {"Candidate": [{"id": 5, "name": "Jane Doe", "email": None}]}
        hits = self.client.search_by_name("Jane Anne Doe")
        self.assertEqual(hits[0].id, 5)
        self.assertIsNone(hits[0].email)
        query = [c for c in self.session.calls if c[1].endswith("search/Candidate")][0][2]["query"]
        self.assertIn('firstName:"Jane" AND lastName:"Doe"', query)
        self.assertEqual(self.client.search_by_name(" "), [])

    def test_lucene_quote(self):
        self.assertEqual(lucene_quote('o"brien\\x'), '"o\\"brien\\\\x"')

    def test_job_order_suggestions(self):
        cand = self.client.job_order_suggestions(record())
        self.assertEqual(cand, [{"id": 77, "title": "Head of Sales", "company": "Acme", "status": "Submitted"}])
        contact = self.client.job_order_suggestions(record("ClientContact", 9))
        self.assertEqual(contact[0]["id"], 78)

    def test_create_note_payload(self):
        note_id = self.client.create_note(record(), "Discussed. Next Friday.", "Candidate Call", job_order_id=77)
        self.assertEqual(note_id, 4242)
        method, url, params, headers, body = self.session.calls[-1]
        self.assertEqual((method, url.rsplit("/", 1)[1]), ("PUT", "Note"))
        self.assertEqual(body, {"action": "Candidate Call", "comments": "Discussed. Next Friday.", "personReference": {"id": 101}, "jobOrder": {"id": 77}})
        self.client.create_note(record(), "Discussed. Next Friday.", "Call")
        self.assertNotIn("jobOrder", self.session.calls[-1][4])
        with self.assertRaises(BullhornError):
            self.client.create_note(record(), "   ", "Call")
        self.session.note_response = {"changeType": "INSERT"}
        with self.assertRaises(BullhornError):
            self.client.create_note(record(), "x. y.", "Call")

    def test_http_error_raises(self):
        self.client.login()
        self.client.rest_url = "https://rest.example/rest-services/abc/"
        with self.assertRaises(BullhornError) as ctx:
            self.client._request("GET", "does/not/exist")
        self.assertIn("404", str(ctx.exception))

    def test_authorize_failure_message(self):
        class BadSession(FakeSession):
            def request(self, method, url, params=None, **kw):
                return FakeResponse(200, text="<html>login form</html>")

        client = BullhornClient(make_settings(), session=BadSession())
        with self.assertRaises(BullhornError) as ctx:
            client.login()
        self.assertIn("BULLHORN_USERNAME", str(ctx.exception))

    def test_record_from_data_name_fallback(self):
        rec = record_from_data("Candidate", {"id": 3, "firstName": "A", "lastName": "B"})
        self.assertEqual(rec.name, "A B")


if __name__ == "__main__":
    unittest.main()
