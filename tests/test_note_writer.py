import unittest
from types import SimpleNamespace

from metaview_bullhorn.errors import NoteGenerationError, NoteValidationError
from metaview_bullhorn.note_writer import HOUSE_STYLE_PROMPT, NoteGenerator, build_user_prompt, count_sentences, infer_action_type, validate_note
from tests.helpers import make_conversation, make_settings

GOOD = "Discussed the Head of Sales role and her interest in moving this quarter. She will send availability for a client intro. Next call booked for Friday."


class ValidateNoteTests(unittest.TestCase):
    def test_good_note_passes_and_is_flattened(self):
        self.assertEqual(validate_note("Discussed the role.\n  Next call Friday.  "), "Discussed the role. Next call Friday.")
        self.assertEqual(validate_note(GOOD), GOOD)

    def test_sentence_bounds(self):
        with self.assertRaises(NoteValidationError):
            validate_note("Only one sentence here.")
        with self.assertRaises(NoteValidationError):
            validate_note("One. Two. Three. Four. Five.")
        self.assertEqual(count_sentences("Met Dr. Smith at 3.5pm, e.g. briefly. Next steps agreed."), 2)

    def test_empty(self):
        with self.assertRaises(NoteValidationError):
            validate_note("   \n ")

    def test_contact_details_rejected(self):
        for bad in (
            "Discussed the role. Email jane@example.com to follow up.",
            "Discussed the role. Call her on +44 7700 900123 tomorrow.",
            "Discussed the role. See https://example.com/profile for details.",
            "Discussed the role. Profile at www.example.com next.",
        ):
            with self.assertRaises(NoteValidationError, msg=bad):
                validate_note(bad)

    def test_transcript_markers_rejected(self):
        with self.assertRaises(NoteValidationError):
            validate_note("At 00:12:30 she said the role fits. Next call Friday.")
        with self.assertRaises(NoteValidationError):
            validate_note('She said "I would really love to move into a leadership role within the next six months or so". Next call Friday.')
        with self.assertRaises(NoteValidationError):
            validate_note("Jane: I want to move.\nAnthony: Understood, let us talk Friday.")
        with self.assertRaises(NoteValidationError):
            validate_note("Discussed the role.\n- next call Friday\n- send CV")

    def test_error_lists_all_problems(self):
        with self.assertRaises(NoteValidationError) as ctx:
            validate_note("Email jane@example.com or see https://x.io now.")
        msg = str(ctx.exception)
        self.assertIn("email", msg)
        self.assertIn("link", msg)
        self.assertIn("sentence", msg)


class ActionTypeTests(unittest.TestCase):
    def test_keyword_mapping(self):
        s = make_settings()
        self.assertEqual(infer_action_type(make_conversation(title="Client intake: Acme"), s), "Client Call")
        self.assertEqual(infer_action_type(make_conversation(title="Screen - Jane"), s), "Candidate Call")
        self.assertEqual(infer_action_type(make_conversation(title="Final Interview Prep"), s), "Interview")
        self.assertEqual(infer_action_type(make_conversation(title="Jane / Anthony"), s), "Call")
        s2 = make_settings(NOTE_DEFAULT_ACTION="Note", NOTE_ACTION_MAP='{"debrief":"Client Debrief"}')
        self.assertEqual(infer_action_type(make_conversation(title="Debrief with Acme"), s2), "Client Debrief")
        self.assertEqual(infer_action_type(make_conversation(title="Screen - Jane"), s2), "Note")


class FakeAnthropic:
    """Minimal stand-in for anthropic.Anthropic exposing beta.messages.create."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text, stop = reply
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(ANTHROPIC_API_KEY="k")
        self.conv = make_conversation()

    def test_prompt_contains_rules_and_source(self):
        prompt = build_user_prompt(self.conv, "Candidate Call", feedback="too long")
        self.assertIn("Call type: Candidate Call", prompt)
        self.assertIn(self.conv.body, prompt)
        self.assertIn("too long", prompt)
        for phrase in ("every person in the company", "contact details", "personality", "Two to four sentences"):
            self.assertIn(phrase, HOUSE_STYLE_PROMPT)

    def test_disabled_without_key(self):
        gen = NoteGenerator(make_settings())
        self.assertFalse(gen.enabled)
        with self.assertRaises(NoteGenerationError):
            gen.generate(self.conv, "Call")

    def test_valid_first_draft(self):
        client = FakeAnthropic([(GOOD, "end_turn")])
        gen = NoteGenerator(self.settings, client=client)
        self.assertEqual(gen.generate(self.conv, "Candidate Call"), GOOD)
        req = client.requests[0]
        self.assertEqual(req["model"], "claude-opus-5")
        self.assertEqual(req["system"], HOUSE_STYLE_PROMPT)
        self.assertEqual(req["fallbacks"], "default")

    def test_retry_after_validation_failure(self):
        client = FakeAnthropic([("Bad. Contains jane@example.com.", "end_turn"), (GOOD, "end_turn")])
        gen = NoteGenerator(self.settings, client=client)
        self.assertEqual(gen.generate(self.conv, "Call"), GOOD)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("rejected", client.requests[1]["messages"][0]["content"])

    def test_two_bad_drafts_fail(self):
        client = FakeAnthropic([("One.", "end_turn"), ("Still one.", "end_turn")])
        with self.assertRaises(NoteGenerationError):
            NoteGenerator(self.settings, client=client).generate(self.conv, "Call")

    def test_refusal_and_truncation(self):
        with self.assertRaises(NoteGenerationError):
            NoteGenerator(self.settings, client=FakeAnthropic([("", "refusal")])).generate(self.conv, "Call")
        with self.assertRaises(NoteGenerationError):
            NoteGenerator(self.settings, client=FakeAnthropic([(GOOD, "max_tokens")])).generate(self.conv, "Call")

    def test_sdk_error_wrapped(self):
        with self.assertRaises(NoteGenerationError):
            NoteGenerator(self.settings, client=FakeAnthropic([RuntimeError("network")])).generate(self.conv, "Call")

    def test_old_sdk_without_fallbacks_kwarg(self):
        client = FakeAnthropic([(GOOD, "end_turn")])
        beta_create = client.beta.messages.create

        def strict_beta(**kwargs):
            if "fallbacks" in kwargs:
                raise TypeError("unexpected keyword argument 'fallbacks'")
            return beta_create(**kwargs)

        client.beta.messages.create = strict_beta
        self.assertEqual(NoteGenerator(self.settings, client=client).generate(self.conv, "Call"), GOOD)
        self.assertNotIn("fallbacks", client.requests[0])


if __name__ == "__main__":
    unittest.main()
