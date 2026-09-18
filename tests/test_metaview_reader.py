import json
import tempfile
import unittest
from pathlib import Path

from metaview_bullhorn.errors import ExtractionError
from metaview_bullhorn.metaview_reader import MetaviewReader, conversation_id_from_url, find_email, normalise_datetime, parse_attendee
from metaview_bullhorn.selectors import DEFAULT_SELECTORS, load_selectors
from tests.helpers import make_settings


class HelperTests(unittest.TestCase):
    def test_conversation_id(self):
        self.assertEqual(conversation_id_from_url("https://app.metaview.test/conversations/abc-123?x=1"), "abc-123")
        self.assertEqual(conversation_id_from_url("/meetings/m_9"), "m_9")
        with self.assertRaises(ExtractionError) as ctx:
            conversation_id_from_url("https://app.metaview.test/settings")
        self.assertEqual(ctx.exception.field, "id")

    def test_find_email(self):
        self.assertEqual(find_email("mailto:Jane@Example.com"), "jane@example.com")
        self.assertIsNone(find_email("no email here"))
        self.assertIsNone(find_email(None))

    def test_normalise_datetime(self):
        self.assertEqual(normalise_datetime("2026-09-17T10:30:00Z", "ignored"), "2026-09-17T10:30+00:00")
        self.assertEqual(normalise_datetime(None, " 17 Sep 2026, 10:30 "), "17 Sep 2026, 10:30")
        with self.assertRaises(ExtractionError):
            normalise_datetime(None, "  ")

    def test_parse_attendee(self):
        a = parse_attendee("Jane Doe", "mailto:jane@example.com", "<li>Jane Doe</li>")
        self.assertEqual((a.name, a.email), ("Jane Doe", "jane@example.com"))
        b = parse_attendee("Jane Doe jane@example.com", None, None)
        self.assertEqual((b.name, b.email), ("Jane Doe", "jane@example.com"))
        c = parse_attendee("Jane Doe", None, '<li title="jane@example.com">Jane Doe</li>')
        self.assertEqual(c.email, "jane@example.com")
        d = parse_attendee("Jane Doe", None, "<li>Jane Doe</li>")
        self.assertIsNone(d.email)
        with self.assertRaises(ExtractionError) as ctx:
            parse_attendee("  ", None, None)
        self.assertEqual(ctx.exception.field, "attendee.name")

    def test_load_selectors_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sel.json"
            path.write_text(json.dumps({"detail.title": "h2.title", "detail.notes": ["#notes"]}))
            sel = load_selectors(path)
            self.assertEqual(sel["detail.title"], ["h2.title"])
            self.assertEqual(sel["detail.notes"], ["#notes"])
            self.assertEqual(sel["list.links"], DEFAULT_SELECTORS["list.links"])
            path.write_text(json.dumps({"bogus": "x"}))
            with self.assertRaises(ValueError):
                load_selectors(path)
            path.write_text(json.dumps({"detail.title": [""]}))
            with self.assertRaises(ValueError):
                load_selectors(path)


# --- A tiny fake DOM so fetch_conversation can be exercised without a browser ---


class FakeElement:
    def __init__(self, text="", attrs=None, children=None, html=None):
        self.text = text
        self.attrs = attrs or {}
        self.children = children or {}  # selector -> list[FakeElement]
        self.html = html or f"<el>{text}</el>"

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)

    def evaluate(self, _script):
        return self.html

    def locator(self, selector):
        return FakeLocator(self.children.get(selector, []))


class FakeLocator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    @property
    def first(self):
        return self.elements[0]

    def all(self):
        return list(self.elements)


class FakePage(FakeElement):
    def __init__(self, children, url="https://app.metaview.test/conversations/c1"):
        super().__init__(children=children)
        self.url = url
        self.visited = []

    def goto(self, url, wait_until=None):
        self.visited.append(url)
        self.url = url

    def wait_for_load_state(self, *a, **k):
        pass


def detail_page(**overrides):
    children = {
        "[data-testid='conversation-title']": [FakeElement("Screen - Jane Doe")],
        "time[datetime]": [FakeElement("17 Sep", attrs={"datetime": "2026-09-17T10:00:00Z"})],
        "[data-testid='attendee']": [
            FakeElement("Anthony Erondu", children={"a[href^='mailto:']": [FakeElement("", attrs={"href": "mailto:anthony@ourfirm.com"})]}),
            FakeElement("Jane Doe", html='<div title="jane@example.com">Jane Doe</div>'),
        ],
        "[data-testid='notes']": [FakeElement("Jane discussed the role.\n\nNext call Friday.")],
    }
    children.update(overrides)
    return FakePage(children)


def reader_with(page):
    reader = MetaviewReader(make_settings())
    reader.page = page
    return reader


class FetchConversationTests(unittest.TestCase):
    def test_full_extraction(self):
        page = detail_page()
        conv = reader_with(page).fetch_conversation("https://app.metaview.test/conversations/c1")
        self.assertEqual(conv.id, "c1")
        self.assertEqual(conv.title, "Screen - Jane Doe")
        self.assertEqual(conv.occurred_at, "2026-09-17T10:00+00:00")
        self.assertEqual([(a.name, a.email) for a in conv.attendees], [("Anthony Erondu", "anthony@ourfirm.com"), ("Jane Doe", "jane@example.com")])
        self.assertEqual(conv.body, "Jane discussed the role.\nNext call Friday.")
        self.assertEqual(conv.body_source, "notes")

    def test_transcript_fallback(self):
        page = detail_page(**{"[data-testid='notes']": [], "[data-testid='transcript']": [FakeElement("full transcript")]})
        conv = reader_with(page).fetch_conversation("https://app.metaview.test/conversations/c1")
        self.assertEqual((conv.body, conv.body_source), ("full transcript", "transcript"))

    def test_missing_fields_raise_named_errors(self):
        cases = {
            "title": {"[data-testid='conversation-title']": []},
            "occurred_at": {"time[datetime]": []},
            "attendees": {"[data-testid='attendee']": []},
            "body": {"[data-testid='notes']": []},
        }
        for field, override in cases.items():
            with self.assertRaises(ExtractionError, msg=field) as ctx:
                reader_with(detail_page(**override)).fetch_conversation("https://app.metaview.test/conversations/c1")
            self.assertEqual(ctx.exception.field, field)
            self.assertEqual(ctx.exception.conversation, "c1")
            self.assertIn("c1", str(ctx.exception))

    def test_empty_title_element_raises(self):
        page = detail_page(**{"[data-testid='conversation-title']": [FakeElement("   ")]})
        with self.assertRaises(ExtractionError) as ctx:
            reader_with(page).fetch_conversation("https://app.metaview.test/conversations/c1")
        self.assertEqual(ctx.exception.field, "title")
        self.assertIn("empty", ctx.exception.detail)

    def test_list_urls_dedupes_and_limits(self):
        links = [
            FakeElement(attrs={"href": "/conversations/a"}),
            FakeElement(attrs={"href": "/conversations/a?tab=notes"}),
            FakeElement(attrs={"href": "https://app.metaview.test/conversations/b"}),
            FakeElement(attrs={"href": "/settings"}),
            FakeElement(attrs={"href": "/conversations/c"}),
        ]
        page = FakePage({"a[href*='/conversations/']": links}, url="https://app.metaview.test/conversations")
        page.children["input[type='password']"] = []
        urls = reader_with(page).list_conversation_urls(limit=2)
        self.assertEqual(urls, ["https://app.metaview.test/conversations/a", "https://app.metaview.test/conversations/b"])

    def test_logged_out_raises(self):
        page = FakePage({"input[type='password']": [FakeElement()]}, url="https://app.metaview.test/login")
        with self.assertRaises(ExtractionError) as ctx:
            reader_with(page).list_conversation_urls()
        self.assertEqual(ctx.exception.field, "session")

    def test_no_links_raises(self):
        page = FakePage({"input[type='password']": []}, url="https://app.metaview.test/conversations")
        with self.assertRaises(ExtractionError) as ctx:
            reader_with(page).list_conversation_urls()
        self.assertEqual(ctx.exception.field, "list.links")


if __name__ == "__main__":
    unittest.main()
