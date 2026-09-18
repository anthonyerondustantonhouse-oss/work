"""DOM selectors for Metaview. Every entry is a list of CSS selectors tried in
order. These are starting guesses: Metaview's markup is not documented and can
change without notice, so the first build step is `mvsync probe`, which saves
the live HTML and a screenshot so you can adjust these. Override any key by
pointing METAVIEW_SELECTORS_FILE at a JSON file with the same shape.

Rule: a selector that matches nothing raises ExtractionError naming the field.
The reader never silently returns an empty string for a required field.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_SELECTORS: dict[str, list[str]] = {
    # --- conversations list page ("My conversations" at /notes) ---
    # One element per row. Each row shows: thumbnail, title, an attendee line
    # ("Austin Dupuy with Elle Zoma and Anthony Erondu"), a relative date
    # ("Yesterday, 2:06 pm" / "16 September, 3:00 pm") and sometimes an
    # "Unavailable" badge when no conversation was detected.
    "list.rows": [
        "[data-testid='conversation-row']",
        "[role='row']",
        "[role='listitem']",
        "main li",
    ],
    # Within a row: the link to the conversation, the title, the attendee line, the date.
    "row.link": ["a[href*='/notes/']", "a[href*='/conversations/']", "a[href]"],
    "row.title": ["[data-testid='conversation-title']", "h3", "h2", "[class*='title']", "a"],
    "row.subtitle": ["[data-testid='conversation-attendees']", "[class*='subtitle']", "[class*='attendee']", "[class*='participant']", "p"],
    "row.date": ["time", "[data-testid='conversation-date']", "[class*='date']", "[class*='time']"],
    "row.unavailable": ["[data-testid='unavailable']", "[class*='unavailable']"],
    # Fallback when no row selector matches: bare anchors that lead to a conversation.
    "list.links": [
        "a[href*='/notes/']",
        "a[href*='/conversations/']",
        "a[href*='/conversation/']",
        "a[href*='/meetings/']",
    ],
    # --- conversation detail page ---
    # Title, date and attendees fall back to what the list row showed when the
    # detail selectors find nothing; the note body must come from the detail page.
    "detail.title": [
        "[data-testid='conversation-title']",
        "main h1",
        "h1",
    ],
    "detail.datetime": [
        "[data-testid='conversation-date'] time",
        "[data-testid='conversation-date']",
        "main time[datetime]",
        "time[datetime]",
        "time",
    ],
    # One element per attendee.
    "detail.attendees": [
        "[data-testid='attendee']",
        "[data-testid='conversation-attendees']",
        "[data-testid='participant']",
        "[data-testid='attendees'] li",
        "[data-testid='participants'] li",
        "[class*='attendee']",
        "[class*='participant']",
    ],
    # Within an attendee element: name and (optional) email.
    "attendee.name": [
        "[data-testid='attendee-name']",
        "[class*='name']",
        "span",
    ],
    "attendee.email": [
        "a[href^='mailto:']",
        "[data-testid='attendee-email']",
        "[class*='email']",
    ],
    # Metaview's generated notes for the call, preferred over the transcript.
    "detail.notes": [
        "[data-testid='notes']",
        "[data-testid='ai-notes']",
        "[data-testid='summary']",
        "section[aria-label*='Notes' i]",
        "[class*='notes']",
        "[class*='summary']",
    ],
    # Full transcript, used only when there are no generated notes.
    "detail.transcript": [
        "[data-testid='transcript']",
        "section[aria-label*='Transcript' i]",
        "[class*='transcript']",
    ],
}

# Conversation id is taken from the URL with this pattern (first capture group).
CONVERSATION_ID_PATTERN = r"/(?:notes|conversations?|meetings?)/([A-Za-z0-9_\-]+)"


def load_selectors(path: str | Path | None) -> dict[str, list[str]]:
    selectors = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    if not path:
        return selectors
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("selectors file must be a JSON object")
    for key, value in data.items():
        if key not in DEFAULT_SELECTORS:
            raise ValueError(f"unknown selector key {key!r}; known keys: {sorted(DEFAULT_SELECTORS)}")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"selector {key!r} must be a string or a list of non-empty strings")
        selectors[key] = value
    return selectors
