# Metaview → Bullhorn note sync

Every thirty minutes: read new Metaview conversations, map each one to the right Bullhorn record, draft a note in the house style, and write it to Bullhorn **only after a human confirms the record match**.

Two things drive the design and are not negotiable:

1. **The confirmation gate is a hard requirement.** The previous official Metaview→Bullhorn sync was disabled at the firm because records were mapped incorrectly. Nothing reaches Bullhorn until you press Approve on the local confirmation page. Items wait in the queue indefinitely; they never time out into an automatic write.
2. **Bullhorn notes are company-visible.** The note generator includes only what was discussed and the next meeting. No transcript excerpts, no contact details, no background, no read on the person. Every note, machine- or human-written, is validated against those rules before it can be written.

## Stack

Python 3.11+, Playwright (persistent profile, so the Metaview login survives between runs), the Bullhorn REST API for all writes (stable and auditable), SQLite for local state, APScheduler or cron for the cadence, the Claude API for drafting notes.

## Layout

| File | Component |
|---|---|
| `metaview_bullhorn/metaview_reader.py`, `selectors.py` | 1. Metaview reader (Playwright). Every selector is treated as fragile: a missing field raises `ExtractionError` naming the field. |
| `metaview_bullhorn/store.py` | 2. SQLite `processed` table (dedup + queue) and `runs` table. |
| `metaview_bullhorn/matcher.py`, `bullhorn.py` | 3. Matcher with confidence scoring on top of the Bullhorn REST client. |
| `metaview_bullhorn/confirm_web.py` | 4. Confirmation gate: one-page localhost web view with approve / reject / reassign. |
| `metaview_bullhorn/note_writer.py` | 5. Note writer: house-style prompt, action type inference, validation. |
| `metaview_bullhorn/runner.py`, `alerts.py` | 6. One run with logging, abort-on-error, and alerts after three consecutive failures. |
| `metaview_bullhorn/scheduler.py` | 7. Thirty-minute cadence. |
| `metaview_bullhorn/cli.py` | `mvsync` command line, one subcommand per build step. |
| `tests/` | Unit tests. No network, browser or third-party package needed: `python -m unittest discover -s tests -t .` |

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env      # fill in; .env is git-ignored and must stay out of version control
```

Bullhorn credentials are data-centre specific. Look yours up with `https://rest.bullhornstaffing.com/rest-services/loginInfo?username=<api user>` and set `BULLHORN_AUTH_URL`, `BULLHORN_REST_TOKEN_URL` and `BULLHORN_REST_LOGIN_URL` from the `oauthUrl` / `restUrl` it returns.

## Build order (each step is a subcommand)

1. **Playwright reads the conversation list.** `mvsync login` opens a visible browser on `my.metaview.app/notes`; sign in once, the profile is saved. Then `mvsync read` should print every conversation with title, date, attendees and a body. The reader works from what the list rows visibly show: the title, the attendee line ("Austin Dupuy with Elle Zoma and Anthony Erondu", including an email standing in for a name), and the relative date ("Yesterday, 2:06 pm", "16 September, 3:00 pm"). Rows Metaview marks "Unavailable" (no conversation detected) are skipped. If `read` fails, it names the field; run `mvsync probe`, which saves the live HTML, visible text, a screenshot and every conversation link to `./probe/`, and tune `metaview_bullhorn/selectors.py` (or a JSON file named by `METAVIEW_SELECTORS_FILE`). The reader never returns an empty string for a required field.
2. **SQLite dedup.** Automatic from here on: `mvsync run` records every conversation id in `DB_PATH`. A conversation already in the table (any status) is never processed again; `written` is skipped silently.
3. **Bullhorn auth and a single read.** `mvsync bullhorn-check Candidate:123` authenticates and prints the record.
4. **Matcher.** `mvsync match` reads Metaview and prints, per conversation, the proposed record, confidence and reasoning, without changing any state.
5. **Confirmation queue.** `mvsync run` queues new conversations; `mvsync serve` opens the page at `http://127.0.0.1:8765/`.
6. **Note write.** `mvsync test-note` writes one note to the throwaway record in `BULLHORN_TEST_RECORD` (it asks you to type the record id first) and reads it back. Only after that: approve a real item in the page and run `mvsync write-confirmed`.
7. **Scheduler.** `mvsync schedule` runs every `SYNC_INTERVAL_MINUTES` with the confirmation page served in the same process. Or use cron and serve the page separately:

   ```cron
   */30 * * * * cd /path/to/repo && .venv/bin/mvsync run >> data/cron.out 2>&1
   ```

## How a run works

1. Read every conversation from Metaview up front. Any extraction error aborts the run before anything is matched or written, and the error is logged.
2. For each id not yet in `processed`: match, infer the action type from the title (`NOTE_ACTION_MAP`), draft the note, insert as `pending`. Only an exact-email match with exactly one hit can skip the queue, and only when `REQUIRE_CONFIRMATION_FOR_ALL=false` (default `true`).
3. Write every `confirmed` item. A write failure marks that item `failed`, aborts the run, and the item reappears in the queue with the error shown.
4. Append the run summary to `LOG_PATH` (`RUN ok=… found=… new=… matched=… queued=… written=… errors=…`). After `CONSECUTIVE_FAILURE_ALERT_THRESHOLD` failed runs in a row, send an alert by email (SMTP settings) and/or desktop notification. `mvsync test-alert` checks the channels.

## Matching rules

* Attendees on the firm's side (`INTERNAL_EMAIL_DOMAINS`, `INTERNAL_EMAILS`, `INTERNAL_NAMES`) are never looked up.
* Exact email match (Lucene lookup, then re-checked for exact equality on the returned rows) across Candidate and ClientContact is the **only** high-confidence path, and only when exactly one distinct record matches.
* More than one email hit: low confidence, all offered as alternatives.
* No email or no hit: name search. Every name-based result is low confidence regardless of how good it looks.
* Nothing found: the item still queues so you can reassign it by Bullhorn id; the page verifies the id against Bullhorn before accepting it.

## Statuses

`pending` → waiting for you; `confirmed` → approved, written on the next run (or immediately with `WRITE_ON_APPROVE=true`); `written` → done, terminal; `skipped` → rejected; `failed` → write failed, back in the queue with the error. Only `confirmed` rows can be written, a row with a note id is never written again, and `written` rows cannot be changed.

## House style for notes

The prompt in `note_writer.py` (`HOUSE_STYLE_PROMPT`) and `validate_note` enforce: two to four sentences of plain prose; only what was discussed and the next meeting or agreed next step; no email addresses, phone numbers, links, timestamps, long quotations, bullet points or speaker-labelled lines. The page lets you edit the draft before approving; the same validation applies to the edited text. Leave `ANTHROPIC_API_KEY` empty to write every note by hand.

Note drafting uses `claude-opus-5` with server-side refusal fallbacks enabled, so a declined request is retried on a fallback model inside the same call. Change `NOTE_MODEL` to use a different model.

## Logging and failure

Everything goes to `LOG_PATH` (and stderr). A run never raises: the outcome is recorded in the `runs` table and the log. Partial extraction never becomes a note, because extraction either succeeds for every field or aborts the run.
