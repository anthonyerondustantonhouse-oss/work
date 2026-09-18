"""Component one: read conversations from Metaview with Playwright.

Uses a persistent Chromium profile so the login survives between runs. All
DOM access goes through helpers that raise ExtractionError naming the field,
so a layout change fails the run loudly instead of producing a blank note.

What Metaview shows (my.metaview.app/notes, "My conversations"): one row per
conversation with a title ("Jackie Fredette and Anthony Erondu"), an attendee
line ("Austin Dupuy with Elle Zoma and Anthony Erondu"), a relative date
("Yesterday, 2:06 pm", "16 September, 3:00 pm") and, for meetings where no
conversation was detected, an "Unavailable" badge. The list row is read first;
the detail page supplies the note body and, when its own selectors match,
overrides the title, date and attendees.

Playwright is imported lazily so the rest of the package (and the tests) work
without it installed.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin

from .config import Settings
from .errors import ExtractionError
from .models import Attendee, Conversation
from .selectors import CONVERSATION_ID_PATTERN, load_selectors

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CONVERSATION_ID_RE = re.compile(CONVERSATION_ID_PATTERN)
UNAVAILABLE_MARKERS = ("unavailable", "couldn't detect any conversation", "could not detect any conversation")
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


# --------------------------------------------------------------------------
# Pure helpers (unit tested without a browser)
# --------------------------------------------------------------------------


def conversation_id_from_url(url: str) -> str:
    match = CONVERSATION_ID_RE.search(url)
    if not match:
        raise ExtractionError("id", f"could not find a conversation id in url {url!r}")
    return match.group(1)


def find_email(text: str | None) -> str | None:
    if not text:
        return None
    match = EMAIL_RE.search(text)
    return match.group(0).lower() if match else None


def clean_text(text: str | None) -> str:
    if text is None:
        return ""
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def parse_attendee(name_text: str | None, email_hint: str | None, raw_html: str | None) -> Attendee:
    """Build an Attendee from the pieces found for one person.

    The name is required. The email is optional and is taken from, in order:
    a mailto/email element, an email inside the name text (Metaview shows
    "Deant@malli.com Mallis" when a calendar invite had no display name), or
    an email anywhere in the element's HTML (title/data attributes included).
    """
    name = clean_text(name_text)
    email = find_email(email_hint) or find_email(name) or find_email(raw_html)
    if email and email in name.lower():
        name = re.sub(re.escape(email), "", name, flags=re.IGNORECASE).strip(" -|<>(),")
        if not name:
            name = email.split("@", 1)[0]
    if not name:
        raise ExtractionError("attendee.name", "attendee element had no visible name")
    return Attendee(name=name, email=email)


def looks_like_attendee_line(text: str | None) -> bool:
    """"A with B", "A and B", "A, B, C": people, not a meeting title like "Tuesday Kick Off"."""
    text = text or ""
    return " with " in text or " and " in text or "," in text


def parse_attendee_line(line: str | None) -> list[Attendee]:
    """Split Metaview's attendee line into attendees.

    Handles "A with B", "A with B and C", "A, B, C and D", a trailing "..."
    when the list page truncates, and emails standing in for names.
    """
    text = clean_text(line).replace("\n", " ")
    text = re.sub(r"\s*(\.\.\.|…)\s*$", "", text)
    if not text:
        return []
    parts = re.split(r"\s+with\s+|\s*,\s*|\s+and\s+|\s*&\s*", text)
    attendees: list[Attendee] = []
    seen: set[str] = set()
    for part in parts:
        part = part.strip(" .")
        if not part:
            continue
        attendee = parse_attendee(part, None, None)
        key = attendee.name.lower()
        if key not in seen:
            seen.add(key)
            attendees.append(attendee)
    return attendees


def parse_metaview_date(text: str | None, now: datetime | None = None) -> str | None:
    """Turn Metaview's relative date text into an ISO timestamp.

    "Yesterday, 2:06 pm" / "Today, 9:30 am" / "16 September, 3:00 pm" /
    "16 September 2025, 3:00 pm" / ISO strings. Returns None when the text is
    not recognised so the caller can keep the raw text instead.
    """
    if not text or not text.strip():
        return None
    value = " ".join(text.split()).strip()
    now = now or datetime.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="minutes")
    except ValueError:
        pass
    m = re.match(r"^(today|yesterday)\s*,?\s*(\d{1,2}):(\d{2})\s*([ap]m)?$", value, re.IGNORECASE)
    if m:
        day = now.date() - timedelta(days=1 if m.group(1).lower() == "yesterday" else 0)
        return datetime.combine(day, _time(m.group(2), m.group(3), m.group(4))).isoformat(timespec="minutes")
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?(?:\s+(\d{4}))?\s*,?\s*(\d{1,2}):(\d{2})\s*([ap]m)?$", value, re.IGNORECASE)
    if m and m.group(2).lower() in MONTHS:
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            when = datetime(year, month, day, *_hm(m.group(4), m.group(5), m.group(6)))
        except ValueError:
            return None
        if not m.group(3) and when > now + timedelta(days=1):
            when = when.replace(year=year - 1)  # no year shown and the date is in the future: it was last year
        return when.isoformat(timespec="minutes")
    return None


def _hm(hour: str, minute: str, ampm: str | None) -> tuple[int, int]:
    h, mnt = int(hour), int(minute)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
    return h, mnt


def _time(hour: str, minute: str, ampm: str | None):
    from datetime import time

    return time(*_hm(hour, minute, ampm))


def normalise_datetime(attr_value: str | None, text_value: str | None, now: datetime | None = None) -> str:
    """Prefer a machine-readable datetime attribute; then Metaview's relative
    text; then the raw visible text. Never returns an empty string."""
    for candidate in (attr_value, text_value):
        if not candidate or not candidate.strip():
            continue
        parsed = parse_metaview_date(candidate, now)
        return parsed if parsed else candidate.strip()
    raise ExtractionError("occurred_at", "no datetime attribute or text found")


def is_unavailable_text(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in UNAVAILABLE_MARKERS)


@dataclass
class ConversationSummary:
    """What one row of the list page tells us."""

    id: str
    url: str
    title: str
    attendee_line: str
    date_text: str
    unavailable: bool = False


# --------------------------------------------------------------------------
# Playwright reader
# --------------------------------------------------------------------------


class MetaviewReader:
    """Context manager around a persistent Playwright browser context."""

    def __init__(self, settings: Settings, headless: bool | None = None):
        self.settings = settings
        self.headless = settings.playwright_headless if headless is None else headless
        self.selectors = load_selectors(settings.metaview_selectors_file)
        self._pw = None
        self._context = None
        self.page = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "MetaviewReader":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment specific
            raise RuntimeError("playwright is not installed: pip install playwright && playwright install chromium") from exc
        profile = Path(self.settings.playwright_profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            str(profile),
            headless=self.headless,
            viewport={"width": 1400, "height": 1000},
        )
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # -- navigation -------------------------------------------------------

    def goto(self, url: str) -> None:
        assert self.page is not None
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 - networkidle is best effort on SPAs
            pass

    def is_logged_in(self) -> bool:
        """Heuristic: a login form or an auth URL means we're logged out."""
        assert self.page is not None
        url = self.page.url.lower()
        if any(marker in url for marker in ("/login", "sign-in", "signin", "/auth")):
            return False
        return self.page.locator("input[type='password']").count() == 0

    def ensure_logged_in(self) -> None:
        self.goto(self.settings.metaview_conversations_url)
        if not self.is_logged_in():
            raise ExtractionError(
                "session",
                "Metaview is not logged in. Run `mvsync login` and sign in once in the browser window.",
            )

    # -- extraction helpers ----------------------------------------------

    def _first_matching(self, root: Any, key: str):
        """Return the first locator (among the configured selectors) that matches at least once."""
        for selector in self.selectors[key]:
            locator = root.locator(selector)
            try:
                if locator.count() > 0:
                    return locator
            except Exception as exc:  # noqa: BLE001 - invalid selector etc.
                log.debug("selector %r for %s errored: %s", selector, key, exc)
        return None

    def _text(self, root: Any, key: str) -> str:
        locator = self._first_matching(root, key)
        return clean_text(locator.first.inner_text()) if locator is not None else ""

    def extract_text(self, root: Any, key: str, field: str, conversation: str | None, required: bool = True) -> str:
        locator = self._first_matching(root, key)
        if locator is None:
            if required:
                raise ExtractionError(field, f"none of the selectors {self.selectors[key]} matched", conversation)
            return ""
        text = clean_text(locator.first.inner_text())
        if not text and required:
            raise ExtractionError(field, f"selector matched but the element was empty ({self.selectors[key]})", conversation)
        return text

    # -- list page --------------------------------------------------------

    def list_conversations(self, limit: int | None = None) -> list[ConversationSummary]:
        """Read the list rows, newest first as rendered. Rows marked Unavailable
        (no conversation detected) are returned with unavailable=True."""
        assert self.page is not None
        self.ensure_logged_in()
        rows = self._first_matching(self.page, "list.rows")
        summaries: list[ConversationSummary] = []
        seen: set[str] = set()

        if rows is not None:
            for row in rows.all():
                link = self._first_matching(row, "row.link")
                href = link.first.get_attribute("href") if link is not None else None
                if not href:
                    continue
                url = urljoin(self.settings.metaview_base_url, href)
                try:
                    conv_id = conversation_id_from_url(url)
                except ExtractionError:
                    continue
                if conv_id in seen:
                    continue
                row_text = clean_text(row.inner_text())
                lines = row_text.split("\n")
                title = self._text(row, "row.title") or (lines[0] if lines else "")
                subtitle = self._text(row, "row.subtitle")
                if not subtitle or subtitle == title:
                    subtitle = next((ln for ln in lines[1:] if looks_like_attendee_line(ln)), "")
                date_locator = self._first_matching(row, "row.date")
                date_text = ""
                if date_locator is not None:
                    date_text = date_locator.first.get_attribute("datetime") or clean_text(date_locator.first.inner_text())
                if not date_text:
                    date_text = next((ln for ln in lines if parse_metaview_date(ln)), "")
                unavailable = self._first_matching(row, "row.unavailable") is not None or is_unavailable_text(row_text)
                seen.add(conv_id)
                summaries.append(ConversationSummary(conv_id, url, title, subtitle, date_text, unavailable))
                if limit and len(summaries) >= limit:
                    break

        if not summaries:
            # No row selector matched: fall back to bare links so the run can still proceed.
            links = self._first_matching(self.page, "list.links")
            if links is None:
                raise ExtractionError(
                    "list.rows",
                    f"no conversation rows or links found with selectors {self.selectors['list.rows']} / {self.selectors['list.links']}. "
                    "Run `mvsync probe` and adjust the selectors.",
                )
            for anchor in links.all():
                href = anchor.get_attribute("href")
                if not href:
                    continue
                url = urljoin(self.settings.metaview_base_url, href)
                try:
                    conv_id = conversation_id_from_url(url)
                except ExtractionError:
                    continue
                if conv_id in seen:
                    continue
                seen.add(conv_id)
                summaries.append(ConversationSummary(conv_id, url, clean_text(anchor.inner_text()), "", ""))
                if limit and len(summaries) >= limit:
                    break
        if not summaries:
            raise ExtractionError("list.rows", "conversation selectors matched but no hrefs contained a conversation id")
        return summaries

    def list_conversation_urls(self, limit: int | None = None) -> list[str]:
        return [s.url for s in self.list_conversations(limit)]

    # -- detail page ------------------------------------------------------

    def fetch_conversation(self, url: str, summary: ConversationSummary | None = None) -> Conversation:
        """Open the detail page and build a Conversation. The list-row summary
        supplies title, date and attendees when the detail selectors find
        nothing; the note body must come from the detail page."""
        assert self.page is not None
        conv_id = conversation_id_from_url(url)
        self.goto(url)
        page = self.page

        title = self.extract_text(page, "detail.title", "title", conv_id, required=False) or (summary.title if summary else "")
        if not title:
            raise ExtractionError("title", f"none of the selectors {self.selectors['detail.title']} matched and the list row had no title", conv_id)

        dt_locator = self._first_matching(page, "detail.datetime")
        dt_attr = dt_locator.first.get_attribute("datetime") if dt_locator is not None else None
        dt_text = clean_text(dt_locator.first.inner_text()) if dt_locator is not None else ""
        if not dt_attr and not dt_text and summary:
            dt_text = summary.date_text
        try:
            occurred_at = normalise_datetime(dt_attr, dt_text)
        except ExtractionError as exc:
            raise ExtractionError(exc.field, exc.detail + " (detail page and list row)", conv_id) from exc

        attendees = self._detail_attendees(page, conv_id)
        if not attendees and summary:
            attendees = parse_attendee_line(summary.attendee_line)
            if not attendees and looks_like_attendee_line(summary.title):
                attendees = parse_attendee_line(summary.title)
        if not attendees:
            raise ExtractionError("attendees", f"none of the selectors {self.selectors['detail.attendees']} matched and the list row had no attendee line", conv_id)

        body_source = "notes"
        body = self.extract_text(page, "detail.notes", "notes", conv_id, required=False)
        if not body:
            body_source = "transcript"
            body = self.extract_text(page, "detail.transcript", "transcript", conv_id, required=False)
        if not body:
            raise ExtractionError("body", "neither generated notes nor a transcript were found", conv_id)

        return Conversation(
            id=conv_id,
            url=url,
            title=title,
            occurred_at=occurred_at,
            attendees=attendees,
            body=body,
            body_source=body_source,
        )

    def _detail_attendees(self, page: Any, conv_id: str) -> list[Attendee]:
        locator = self._first_matching(page, "detail.attendees")
        if locator is None:
            return []
        elements = locator.all()
        if len(elements) == 1:
            # A single element is an attendee line ("A with B and C"), not one attendee.
            text = clean_text(elements[0].inner_text())
            if looks_like_attendee_line(text):
                return parse_attendee_line(text)
        attendees: list[Attendee] = []
        for element in elements:
            name_loc = self._first_matching(element, "attendee.name")
            name_text = name_loc.first.inner_text() if name_loc is not None else element.inner_text()
            email_hint = None
            email_loc = self._first_matching(element, "attendee.email")
            if email_loc is not None:
                email_hint = email_loc.first.get_attribute("href") or email_loc.first.inner_text()
            raw_html = element.evaluate("el => el.outerHTML")
            try:
                attendees.append(parse_attendee(name_text, email_hint, raw_html))
            except ExtractionError as exc:
                raise ExtractionError(exc.field, exc.detail, conv_id) from exc
        return attendees

    def read_all(self, limit: int | None = None) -> list[Conversation]:
        limit = limit or self.settings.metaview_max_conversations
        conversations: list[Conversation] = []
        for summary in self.list_conversations(limit):
            if summary.unavailable:
                log.info("skipping %s (%s): Metaview marked it unavailable, no conversation detected", summary.id, summary.title)
                continue
            conversations.append(self.fetch_conversation(summary.url, summary))
        return conversations

    # -- diagnostics ------------------------------------------------------

    def probe(self, out_dir: Path, url: str | None = None) -> Path:
        """Save the page HTML, visible text, a screenshot, and every
        conversation-looking link so the selectors can be tuned against real markup."""
        assert self.page is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        self.goto(url or self.settings.metaview_conversations_url)
        (out_dir / "page.html").write_text(self.page.content(), encoding="utf-8")
        (out_dir / "page.txt").write_text(self.page.locator("body").inner_text(), encoding="utf-8")
        self.page.screenshot(path=str(out_dir / "page.png"), full_page=True)
        links = []
        for anchor in self.page.locator("a[href]").all():
            href = anchor.get_attribute("href") or ""
            if CONVERSATION_ID_RE.search(href):
                links.append(urljoin(self.settings.metaview_base_url, href))
        (out_dir / "links.txt").write_text("\n".join(dict.fromkeys(links)), encoding="utf-8")
        return out_dir


@contextmanager
def open_reader(settings: Settings, headless: bool | None = None) -> Iterator[MetaviewReader]:
    with MetaviewReader(settings, headless=headless) as reader:
        yield reader
