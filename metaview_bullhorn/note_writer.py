"""Component five: draft the Bullhorn note in the house style, and validate
every draft (machine- or human-written) before it can be written.

House style (Bullhorn notes are visible to the whole company):
  * only what was discussed and the next meeting / agreed next step
  * two to four sentences of plain prose
  * no transcript excerpts, no contact details, no background, no read on the
    person's personality or character

The Anthropic SDK is imported lazily; without an API key the draft is left
empty and must be written by hand in the confirmation page.
"""

from __future__ import annotations

import logging
import re

from .config import Settings
from .errors import NoteGenerationError, NoteValidationError
from .models import Conversation

log = logging.getLogger(__name__)

MAX_NOTE_CHARS = 800
MIN_SENTENCES = 2
MAX_SENTENCES = 4

HOUSE_STYLE_PROMPT = """You write CRM notes for a recruitment firm. The note goes into Bullhorn, where every person in the company can read it, so it must be safe to be seen by anyone.

Write a note about the conversation you are given. Rules:
- Cover only two things: what was discussed, and the next meeting or agreed next step. If no next step was agreed, say so in a short clause.
- Two to four sentences of plain prose. No headings, no bullet points, no labels, no preamble, no sign-off.
- Past tense, matter of fact, third person ("Discussed...", "They confirmed...", "Next call booked for...").
- Never quote or paraphrase the transcript line by line. Summarise.
- Never include contact details: no email addresses, phone numbers, addresses, or links.
- Never include background: no CV history, employer lists, personal circumstances, or how the person came to us, unless it was the subject of the discussion and is needed to understand the next step.
- Never describe the person's personality, character, mood, communication style, or how they came across.
- Do not name the recruiter or the internal attendees.
- Output only the note text."""


def infer_action_type(conversation: Conversation, settings: Settings) -> str:
    """Pick the Bullhorn action type from keywords in the title. First keyword
    (in configured order) that appears in the title wins."""
    title = conversation.title.lower()
    for keyword, action in settings.note_action_map.items():
        if keyword.lower() in title:
            return action
    return settings.note_default_action


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().\-]{7,}\d)")
TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
QUOTE_RE = re.compile(r"[\"“”‘’']([^\"“”‘’']{40,})[\"“”‘’']")
LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|#+)\s+", re.MULTILINE)
SPEAKER_LINE_RE = re.compile(r"^\s*[A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-]+)?:\s", re.MULTILINE)
ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "mr.", "mrs.", "ms.", "dr.", "vs.", "approx.", "no.")


def count_sentences(text: str) -> int:
    scrubbed = text
    for abbr in ABBREVIATIONS:
        scrubbed = re.sub(re.escape(abbr), abbr.replace(".", ""), scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub(r"\d\.\d", "00", scrubbed)  # decimals like 3.5
    parts = re.split(r"(?<=[.!?])\s+", scrubbed.strip())
    return len([p for p in parts if p.strip()])


def validate_note(text: str) -> str:
    """Return the cleaned note or raise NoteValidationError with every problem found."""
    note = " ".join(line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip())
    problems: list[str] = []
    if not note:
        raise NoteValidationError("note is empty")
    if len(note) > MAX_NOTE_CHARS:
        problems.append(f"note is {len(note)} characters; maximum is {MAX_NOTE_CHARS}")
    sentences = count_sentences(note)
    if sentences < MIN_SENTENCES or sentences > MAX_SENTENCES:
        problems.append(f"note has {sentences} sentence(s); it must have {MIN_SENTENCES} to {MAX_SENTENCES}")
    if EMAIL_RE.search(note):
        problems.append("note contains an email address")
    if URL_RE.search(note):
        problems.append("note contains a link")
    if PHONE_RE.search(note):
        problems.append("note contains what looks like a phone number")
    if TIMESTAMP_RE.search(note):
        problems.append("note contains a timestamp (transcript marker)")
    if QUOTE_RE.search(note):
        problems.append("note contains a long quotation (transcript excerpt)")
    if LIST_LINE_RE.search(text or ""):
        problems.append("note contains bullet points or headings")
    if SPEAKER_LINE_RE.search(text or ""):
        problems.append("note contains speaker-labelled lines (transcript excerpt)")
    if problems:
        raise NoteValidationError("; ".join(problems))
    return note


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def build_user_prompt(conversation: Conversation, action_type: str, feedback: str | None = None) -> str:
    external = ", ".join(a.name for a in conversation.attendees) or "unknown"
    source = "Metaview's generated notes" if conversation.body_source == "notes" else "the call transcript"
    parts = [
        f"Call type: {action_type}",
        f"Title: {conversation.title}",
        f"Date: {conversation.occurred_at}",
        f"Attendees: {external}",
        f"Source material ({source}):",
        "<source>",
        conversation.body,
        "</source>",
    ]
    if feedback:
        parts.append(f"Your previous draft was rejected: {feedback}. Write a new note that fixes this.")
    return "\n".join(parts)


class NoteGenerator:
    """Drafts notes with the Claude API. `generate` returns a validated note."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.anthropic_api_key) or self._client is not None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - environment specific
                raise NoteGenerationError("anthropic SDK is not installed: pip install anthropic") from exc
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def _call(self, user_prompt: str) -> str:
        client = self._get_client()
        kwargs = dict(
            model=self.settings.note_model,
            max_tokens=1024,
            system=HOUSE_STYLE_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        try:
            # Server-side refusal fallbacks: if the primary model declines, the API
            # re-runs on a fallback model inside the same call.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        except TypeError:
            # Older SDK without the fallbacks parameter.
            response = client.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            raise NoteGenerationError("the model declined to draft this note")
        if response.stop_reason == "max_tokens":
            raise NoteGenerationError("the model's draft was cut off")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise NoteGenerationError("the model returned no text")
        return text.strip()

    def generate(self, conversation: Conversation, action_type: str) -> str:
        if not self.enabled:
            raise NoteGenerationError("ANTHROPIC_API_KEY is not set; write the note by hand in the confirmation page")
        try:
            draft = self._call(build_user_prompt(conversation, action_type))
        except NoteGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK/network errors
            raise NoteGenerationError(f"note generation failed: {exc}") from exc
        try:
            return validate_note(draft)
        except NoteValidationError as first:
            log.info("draft rejected (%s); asking for a rewrite", first)
            try:
                draft = self._call(build_user_prompt(conversation, action_type, feedback=str(first)))
            except NoteGenerationError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise NoteGenerationError(f"note generation failed on retry: {exc}") from exc
            try:
                return validate_note(draft)
            except NoteValidationError as second:
                raise NoteGenerationError(f"draft failed validation twice: {second}") from second
