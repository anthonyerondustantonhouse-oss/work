"""Component one: read conversations from Metaview with Playwright.

Uses a persistent Chromium profile so the login survives between runs. All
DOM access goes through `extract_*` helpers that raise ExtractionError naming
the field, so a layout change fails the run loudly instead of producing a
blank note.

Playwright is imported lazily so the rest of the package (and the tests) work
without it installed.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime
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


def normalise_datetime(attr_value: str | None, text_value: str | None) -> str:
    """Prefer a machine-readable datetime attribute; fall back to the visible text."""
    for candidate in (attr_value, text_value):
        if not candidate or not candidate.strip():
            continue
        value = candidate.strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.isoformat(timespec="minutes")
        except ValueError:
            return value
    raise ExtractionError("occurred_at", "no datetime attribute or text found")


def clean_text(text: str | None) -> str:
    if text is None:
        return ""
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def parse_attendee(name_text: str | None, email_hint: str | None, raw_html: str | None) -> Attendee:
    """Build an Attendee from the pieces found in one attendee element.

    The name is required. The email is optional and is taken from, in order:
    a mailto/email element, an email inside the name text, or an email found
    anywhere in the element's HTML (title/data attributes included).
    """
    name = clean_text(name_text)
    email = find_email(email_hint) or find_email(name) or find_email(raw_html)
    if email and email in name.lower():
        # Name element rendered as "Jane Doe jane@x.com": strip the email from the name.
        name = re.sub(re.escape(email), "", name, flags=re.IGNORECASE).strip(" -|<>()")
    if not name:
        raise ExtractionError("attendee.name", "attendee element had no visible name")
    return Attendee(name=name, email=email)


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
        """Heuristic: on the conversations page, a login form means we're logged out."""
        assert self.page is not None
        url = self.page.url.lower()
        if "login" in url or "sign-in" in url or "signin" in url or "auth" in url:
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

    def list_conversation_urls(self, limit: int | None = None) -> list[str]:
        """Return absolute URLs of conversations on the list page, newest first as rendered."""
        assert self.page is not None
        self.ensure_logged_in()
        locator = self._first_matching(self.page, "list.links")
        if locator is None:
            raise ExtractionError("list.links", f"no conversation links found with selectors {self.selectors['list.links']}")
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in locator.all():
            href = anchor.get_attribute("href")
            if not href:
                continue
            absolute = urljoin(self.settings.metaview_base_url, href)
            try:
                conv_id = conversation_id_from_url(absolute)
            except ExtractionError:
                continue
            if conv_id in seen:
                continue
            seen.add(conv_id)
            urls.append(absolute)
            if limit and len(urls) >= limit:
                break
        if not urls:
            raise ExtractionError("list.links", "conversation link selector matched but no hrefs contained a conversation id")
        return urls

    # -- detail page ------------------------------------------------------

    def fetch_conversation(self, url: str) -> Conversation:
        assert self.page is not None
        conv_id = conversation_id_from_url(url)
        self.goto(url)
        page = self.page

        title = self.extract_text(page, "detail.title", "title", conv_id)

        dt_locator = self._first_matching(page, "detail.datetime")
        if dt_locator is None:
            raise ExtractionError("occurred_at", f"none of the selectors {self.selectors['detail.datetime']} matched", conv_id)
        dt_attr = dt_locator.first.get_attribute("datetime")
        dt_text = dt_locator.first.inner_text()
        try:
            occurred_at = normalise_datetime(dt_attr, dt_text)
        except ExtractionError as exc:
            raise ExtractionError(exc.field, exc.detail, conv_id) from exc

        attendee_locator = self._first_matching(page, "detail.attendees")
        if attendee_locator is None:
            raise ExtractionError("attendees", f"none of the selectors {self.selectors['detail.attendees']} matched", conv_id)
        attendees: list[Attendee] = []
        for element in attendee_locator.all():
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
        if not attendees:
            raise ExtractionError("attendees", "attendee selector matched but yielded no attendees", conv_id)

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

    def read_all(self, limit: int | None = None) -> list[Conversation]:
        limit = limit or self.settings.metaview_max_conversations
        return [self.fetch_conversation(url) for url in self.list_conversation_urls(limit)]

    # -- diagnostics ------------------------------------------------------

    def probe(self, out_dir: Path, url: str | None = None) -> Path:
        """Save the page HTML, a screenshot, and every conversation-looking link
        so the selectors can be tuned against real markup."""
        assert self.page is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        self.goto(url or self.settings.metaview_conversations_url)
        (out_dir / "page.html").write_text(self.page.content(), encoding="utf-8")
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
