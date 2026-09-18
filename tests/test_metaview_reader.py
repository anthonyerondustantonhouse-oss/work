import json
import tempfile
import unittest
from pathlib import Path

from metaview_bullhorn.errors import ExtractionError
from datetime import datetime

from metaview_bullhorn.metaview_reader import (
    ConversationSummary,
    looks_like_attendee_line,
    MetaviewReader,
    conversation_id_from_url,
    find_email,
    is_unavailable_text,
    normalise_datetime,
    parse_attendee,
    parse_attendee_line,
    parse_metaview_date,
)
from metaview_bullhorn.selectors import DEFAULT_SELECTORS, load_selectors
from tests.helpers import make_settings


class HelperTests(unittest.TestCase):
    def test_conversation_id(self):
        self.assertEqual(conversation_id_from_url("https://app.metaview.test/conversations/abc-123?x=1"), "abc-123")
        self.assertEqual(conversation_id_from_url("/meetings/m_9"), "m_9")
        self.assertEqual(conversation_id_from_url("https://my.metaview.app/notes/n-77?tab=x"), "n-77")
        with self.assertRaises(ExtractionError):
            conversation_id_from_url("https://my.metaview.app/notes?reconnect=true")
        with self.assertRaises(ExtractionError) as ctx:
            conversation_id_from_url("https://app.metaview.test/settings")
        self.assertEqual(ctx.exception.field, "id")

    def test_find_email(self):
        self.assertEqual(find_email("mailto:Jane@Example.com"), "jane@example.com")
        self.assertIsNone(find_email("no email here"))
        self.assertIsNone(find_email(None))

    def test_normalise_datetime(self):
        self.assertEqual(normalise_datetime("2026-09-17T10:30:00Z", "ignored"), "2026-09-17T10:30+00:00")
        self.assertEqual(normalise_datetime(None, " 17 Sep 2026, 10:30 "), "2026-09-17T10:30")
        self.assertEqual(normalise_datetime(None, " last Tuesday "), "last Tuesday")
        with self.assertRaises(ExtractionError):
            normalise_datetime(None, "  ")

    def test_parse_metaview_date(self):
        now = datetime(2026, 9, 18, 0, 2)
        self.assertEqual(parse_metaview_date("Yesterday, 2:06 pm", now), "2026-09-17T14:06")
        self.assertEqual(parse_metaview_date("Today, 9:30 am", now), "2026-09-18T09:30")
        self.assertEqual(parse_metaview_date("Yesterday, 12:15 am", now), "2026-09-17T00:15")
        self.assertEqual(parse_metaview_date("16 September, 3:00 pm", now), "2026-09-16T15:00")
        self.assertEqual(parse_metaview_date("16 Sept, 12:00 pm", now), "2026-09-16T12:00")
        self.assertEqual(parse_metaview_date("2 January, 10:00 am", now), "2026-01-02T10:00")
        self.assertEqual(parse_metaview_date("30 December, 10:00 am", now), "2025-12-30T10:00")
        self.assertEqual(parse_metaview_date("16 September 2025, 3:00 pm", now), "2025-09-16T15:00")
        self.assertEqual(parse_metaview_date("2026-09-17T10:30:00Z", now), "2026-09-17T10:30+00:00")
        self.assertIsNone(parse_metaview_date("31 February, 3:00 pm", now))
        self.assertIsNone(parse_metaview_date("last week", now))
        self.assertIsNone(parse_metaview_date("", now))
        self.assertEqual(normalise_datetime(None, "Yesterday, 2:06 pm", now), "2026-09-17T14:06")
        self.assertEqual(normalise_datetime(None, "some other text", now), "some other text")

    def test_parse_attendee_line(self):
        names = lambda line: [(a.name, a.email) for a in parse_attendee_line(line)]
        self.assertEqual(names("Austin Dupuy with Elle Zoma and Anthony Erondu"), [("Austin Dupuy", None), ("Elle Zoma", None), ("Anthony Erondu", None)])
        self.assertEqual(names("Jackie Fredette with Anthony Erondu"), [("Jackie Fredette", None), ("Anthony Erondu", None)])
        self.assertEqual(names("Deant@malli.com Mallis with Anthony Erondu"), [("Mallis", "deant@malli.com"), ("Anthony Erondu", None)])
        self.assertEqual(names("jane@example.com with Anthony Erondu")[0], ("jane", "jane@example.com"))
        self.assertEqual(
            names("Elle Zoma, Lucas Alvarado, Minesh Patel, Maris Colton, James Warren, Anthony Erondu ..."),
            [("Elle Zoma", None), ("Lucas Alvarado", None), ("Minesh Patel", None), ("Maris Colton", None), ("James Warren", None), ("Anthony Erondu", None)],
        )
        self.assertEqual(names("Zhiwei with Anthony Erondu"), [("Zhiwei", None), ("Anthony Erondu", None)])
        self.assertEqual(names("Anthony Erondu and Anthony Erondu"), [("Anthony Erondu", None)])
        self.assertEqual(parse_attendee_line("   "), [])

    def test_looks_like_attendee_line(self):
        self.assertTrue(looks_like_attendee_line("Jackie Fredette and Anthony Erondu"))
        self.assertTrue(looks_like_attendee_line("Zhiwei with Anthony Erondu"))
        self.assertFalse(looks_like_attendee_line("Tuesday Kick Off"))
        self.assertFalse(looks_like_attendee_line("Austin x Anthony Catch up"))

    def test_unavailable_markers(self):
        self.assertTrue(is_unavailable_text("Dean x Anthony\nUnavailable\nWe couldn't detect any conversation in this meeting."))
        self.assertFalse(is_unavailable_text("Jackie Fredette with Anthony Erondu"))

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
        self.assertIn("list row had no title", ctx.exception.detail)

    def test_summary_fallback_when_detail_selectors_miss(self):
        page = detail_page(**{"[data-testid='conversation-title']": [], "time[datetime]": [], "[data-testid='attendee']": []})
        summary = ConversationSummary("c1", "https://my.metaview.app/notes/c1", "Jackie Fredette and Anthony Erondu", "Jackie Fredette with Anthony Erondu", "Yesterday, 1:20 pm")
        conv = reader_with(page).fetch_conversation(summary.url, summary)
        self.assertEqual(conv.title, "Jackie Fredette and Anthony Erondu")
        self.assertEqual([a.name for a in conv.attendees], ["Jackie Fredette", "Anthony Erondu"])
        self.assertRegex(conv.occurred_at, r"^\d{4}-\d{2}-\d{2}T13:20$")
        # attendees from the title when the row had no attendee line
        summary.attendee_line = ""
        conv = reader_with(detail_page(**{"[data-testid='attendee']": []})).fetch_conversation(summary.url, summary)
        self.assertEqual([a.name for a in conv.attendees], ["Jackie Fredette", "Anthony Erondu"])
        # and still a named error when neither source has attendees
        summary.title = "Tuesday Kick Off"
        with self.assertRaises(ExtractionError) as ctx:
            reader_with(detail_page(**{"[data-testid='attendee']": []})).fetch_conversation(summary.url, summary)
        self.assertEqual(ctx.exception.field, "attendees")

    def test_detail_single_attendee_line_element(self):
        page = detail_page(**{"[data-testid='attendee']": [FakeElement("Austin Dupuy with Elle Zoma and Anthony Erondu")]})
        conv = reader_with(page).fetch_conversation("https://my.metaview.app/notes/c1")
        self.assertEqual([a.name for a in conv.attendees], ["Austin Dupuy", "Elle Zoma", "Anthony Erondu"])

    def test_list_rows(self):
        def row(href, text, unavailable=False):
            children = {"a[href*='/notes/']": [FakeElement(attrs={"href": href})]}
            if unavailable:
                children["[class*='unavailable']"] = [FakeElement("Unavailable")]
            return FakeElement(text, children=children)

        rows = [
            row("/notes/r1", "Deant@malli.com Mallis with Anthony Erondu\nYesterday, 2:06 pm"),
            row("/notes/r2", "Dean x Anthony\nDeant@malli.com Mallis with Anthony Erondu\nUnavailable\nWe couldn't detect any conversation in this meeting.\nYesterday, 2:00 pm", unavailable=True),
            row("/notes/r3", "Austin/ Elle/ Anthony\nAustin Dupuy with Elle Zoma and Anthony Erondu\n16 September, 3:00 pm"),
            row("/notes/r3", "duplicate"),
            FakeElement("row without link"),
        ]
        page = FakePage({"[role='row']": rows, "input[type='password']": []}, url="https://my.metaview.app/notes")
        reader = reader_with(page)
        reader.settings.metaview_base_url = "https://my.metaview.app"
        summaries = reader.list_conversations()
        self.assertEqual([s.id for s in summaries], ["r1", "r2", "r3"])
        self.assertEqual(summaries[0].title, "Deant@malli.com Mallis with Anthony Erondu")
        self.assertEqual(summaries[0].date_text, "Yesterday, 2:06 pm")
        self.assertTrue(summaries[1].unavailable)
        self.assertFalse(summaries[0].unavailable)
        self.assertEqual(summaries[2].title, "Austin/ Elle/ Anthony")
        self.assertEqual(summaries[2].attendee_line, "Austin Dupuy with Elle Zoma and Anthony Erondu")
        self.assertEqual(summaries[2].date_text, "16 September, 3:00 pm")
        self.assertEqual(summaries[2].url, "https://my.metaview.app/notes/r3")
        self.assertEqual(len(reader.list_conversations(limit=2)), 2)

    def test_read_all_skips_unavailable(self):
        class ListOnlyReader(MetaviewReader):
            fetched = []

            def list_conversations(self, limit=None):
                return [
                    ConversationSummary("a", "u/a", "A", "", "", unavailable=False),
                    ConversationSummary("b", "u/b", "B", "", "", unavailable=True),
                ]

            def fetch_conversation(self, url, summary=None):
                self.fetched.append(summary.id)
                return summary

        reader = ListOnlyReader(make_settings())
        self.assertEqual([c.id for c in reader.read_all()], ["a"])
        self.assertEqual(reader.fetched, ["a"])

    def test_list_urls_dedupes_and_limits(self):
        links = [
            FakeElement(attrs={"href": "/conversations/a"}),
            FakeElement(attrs={"href": "/conversations/a?tab=notes"}),
            FakeElement(attrs={"href": "https://app.metaview.test/conversations/b"}),
            FakeElement(attrs={"href": "/settings"}),
            FakeElement(attrs={"href": "/conversations/c"}),
        ]
        page = FakePage({"a[href*='/conversations/']": links, "[role='row']": [], "input[type='password']": []}, url="https://app.metaview.test/conversations")
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
        self.assertEqual(ctx.exception.field, "list.rows")


if __name__ == "__main__":
    unittest.main()
