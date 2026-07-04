# Provenance Guard

A backend service that any creative sharing platform can plug into to classify
submitted text content, score confidence in that classification, surface a
transparency label to users, and handle appeals from creators who believe they've
been misclassified.

---

## Architecture Overview

A submission travels through eight components in sequence before a label is returned.

**1. Rate Limiter** — Flask-Limiter checks the caller's IP against per-IP counters
in memory. Requests over 10/min or 50/hr are rejected with HTTP 429 before any
analysis runs.

**2. Input Validator** — Enforces `text` and `creator_id` fields, and a 50–10,000
character range. Below 50 characters the linguistic signal lacks enough sentences
to compute meaningful statistics.

**3. Signal 1: Linguistic Analyzer** — Runs four statistical measurements on the
raw text (sentence-length burstiness, vocabulary richness, AI transition-phrase
density, average word length) and returns a `linguistic_score` in [0, 1].

**4. Signal 2: LLM Classifier** — Calls Groq (`llama-3.1-8b-instant`) with a
forensic-linguist prompt. Returns an `llm_score` in [0, 1] and a list of observed
signals. Falls back to 0.5 if the API call fails.

**5. Confidence Scorer** — Combines both scores into `combined_score` (weighted
average, LLM 60%) and `confidence` (penalizes inter-signal disagreement).

**6. Label Generator** — Maps `combined_score` and `confidence` to one of three
transparency label variants using a conservative double-gate threshold.

**7. Audit Logger** — Appends a structured JSON record to `audit_log.json`
containing every field needed for human moderator review.

**8. Response** — Returns the full JSON object including `content_id`, `attribution`,
`confidence`, `transparency_label`, and per-signal detail.

---

## Detection Signals

### Signal 1: Linguistic / Statistical Analysis

**What it measures:** Four surface-level statistical properties, combined with fixed
weights: sentence-length coefficient of variation (35%), type-token ratio adjusted
for text length (30%), density of AI-overused transition phrases (25%), and average
word length (10%).

**Why it was chosen:** It requires no external API call, runs deterministically, and
captures patterns that emerge from how language models are trained. Models optimize
for readability, which produces metronomic sentence lengths and repetitive vocabulary.
The transition-phrase list targets direct training artifacts — phrases that appear
disproportionately in model output because they appeared in high-quality training
essays.

**What it misses:**
- Academic and formal human writing — a PhD student's literature review uses
  transition phrases and consistent sentence lengths by convention, scoring
  indistinguishably from AI output on all four features.
- Style mimicry — an AI prompted to write like a casual diary entry can defeat
  every sub-feature.
- Genre effects — poetry with deliberate repetition (anaphora) has very low TTR
  for intentional artistic reasons.
- Short texts — under ~80 words, CV and TTR are statistically unreliable.

---

### Signal 2: LLM Classifier (Groq / llama-3.1-8b-instant)

**What it measures:** The semantic and stylistic texture of the text as judged by a
language model prompted to act as a forensic linguist. It evaluates personal voice,
lived-experience specificity, structural formulaism, and generic vs. idiosyncratic
phrasing.

**Why it was chosen:** Statistics are blind to meaning. The LLM can detect that
"my mother used to leave folded notes inside my lunch box, always the same blue ink"
is the kind of concrete detail only a human with that specific memory would include —
something no surface measurement captures.

**What it misses:**
- AI text prompted toward specificity — an AI instructed to include concrete sensory
  details produces text that looks human to a classifier. This signal will degrade
  as prompting techniques improve.
- Deliberately generic human writing — brand copywriters, technical writers, and
  form-letter authors strip out personal voice intentionally. They score as AI.
- The classifier's own training bias — we're asking an LLM to detect LLM output.
  It may miss patterns from models trained after its knowledge cutoff, and may
  penalize writing styles underrepresented in its training data (non-Western
  narrative conventions, oral storytelling traditions).
- Temperature variance — two calls with the same text can return scores differing
  by ~0.05–0.10 at temperature 0.1.

---

## Confidence Scoring

Confidence is not the same as the AI-probability estimate. It measures how *certain*
the system is, not just which direction the evidence points.

```
combined_score = 0.40 × linguistic_score + 0.60 × llm_score
confidence     = |combined_score − 0.5| × 2 × (1 − |linguistic_score − llm_score|)
```

The first factor measures how far the combined score is from the center of the
scale (0.5 = maximum uncertainty). The second factor penalizes inter-signal
disagreement: if one signal says 0.8 and the other says 0.2, confidence collapses
toward zero even though the average is 0.5. This prevents a split verdict from
producing a falsely definitive label.

**Validation — two submissions with meaningfully different confidence scores:**

*Submission A: Casual ramen review (clearly human-written)*
```
Text: "ok so i finally tried that new ramen place downtown and honestly?
underwhelming. the broth was fine but they put WAY too much sodium in it..."

linguistic_score : 0.123   (zero phrase hits, short words, varied sentence lengths)
llm_score        : 0.200   (casual register, specific complaint, no formulaic structure)
combined_score   : 0.169
confidence       : 0.611
label            : uncertain (combined ≤ 0.30 ✓ but confidence just under 0.65 threshold)
```

*Submission B: Borderline formal academic text*
```
Text: "The relationship between monetary policy and asset price inflation has been
extensively studied in the literature. Central banks face a fundamental tension..."

linguistic_score : 0.366   (moderate — no phrase hits but formal word length)
llm_score        : 0.800   (LLM flags formulaic structure, no personal voice)
combined_score   : 0.627
confidence       : 0.143   (signals disagree by 0.43 → disagreement penalty fires)
label            : uncertain
```

Submission A (confidence 0.611) and Submission B (confidence 0.143) have noticeably
different confidence scores despite both returning `uncertain`. Submission A is close
to the human threshold; Submission B has nearly no certainty because the two signals
are far apart. A `combined_score` of 0.51 would produce confidence ~0.02.

---

## Transparency Label Variants

These are the exact strings returned in `transparency_label.text`.

**Thresholds:**
- `high_confidence_ai` → `combined_score >= 0.70` AND `confidence >= 0.65`
- `high_confidence_human` → `combined_score <= 0.30` AND `confidence >= 0.65`
- `uncertain` → everything else (including high combined scores with low confidence)

Both gates must pass simultaneously. A score of 0.90 with confidence of 0.40
(because signals disagreed) still returns `uncertain`.

---

### `high_confidence_ai`

> AI-Generated Content: Our analysis strongly indicates this content was produced by
> an AI writing system. Multiple signals — including structural patterns, vocabulary
> characteristics, and stylistic markers — point to AI authorship. If you wrote this
> yourself and believe this assessment is wrong, you can submit an appeal and a human
> moderator will review your case.

---

### `high_confidence_human`

> Human-Created Content: Our analysis strongly indicates this content was written by
> a human. The writing shows characteristics consistent with authentic human
> authorship: varied sentence rhythm, personal voice, and idiosyncratic word choices.
> No significant AI-generation signals were detected.

---

### `uncertain`

> Origin Uncertain: We could not determine with confidence whether this content was
> written by a human or generated by AI. It may be entirely human-written,
> AI-generated, or a blend of both. Our detection signals gave mixed or weak results.
> Readers should draw their own conclusions, and creators who feel this is inaccurate
> are welcome to submit an appeal.

---

## Rate Limiting

Applied per IP address using Flask-Limiter with in-memory storage.

| Endpoint | Limit | Reasoning |
|---|---|---|
| `POST /submit` | 10/min, 50/hr, 200/day | A creator submitting their own work won't need 10 submissions in a minute. The burst window allows rapid draft iteration; the hourly and daily caps prevent sustained API abuse without burdening real users. |
| `POST /appeal/<id>` | 5/hr | Appeals require a human to write a reasoned explanation. Five per hour is generous for any legitimate user; it stops automated appeal flooding. |

Exceeding a limit returns HTTP 429. The per-IP model accepts one trade-off: users
behind a shared IP (e.g., a university network) may hit limits sooner. This is
acceptable given the primary threat is automated scraping, not human creators.

---

## Audit Log

Every classification decision and every appeal is written to `audit_log.json`.
Classification entries are never deleted; status fields are mutated in-place when
an appeal is filed, and a second `appeal` event is appended.

Retrieve: `GET /log`

### Sample entries (3 events)

```json
[
  {
    "event": "classification",
    "content_id": "6eb70a25-aaae-4850-b51b-21ca71841977",
    "creator_id": "creator-anna",
    "timestamp": "2026-07-03T05:46:13.608272Z",
    "text_preview": "ok so i finally tried that new ramen place downtown and honestly?...",
    "attribution": "Human-written",
    "label_variant": "uncertain",
    "signals": {
      "linguistic_score": 0.1233,
      "llm_score": 0.4,
      "combined_score": 0.2893,
      "confidence": 0.3048
    },
    "status": "classified"
  },
  {
    "event": "classification",
    "content_id": "9b3cb643-d597-48d0-b736-f3dde8e34f5b",
    "creator_id": "creator-bob",
    "timestamp": "2026-07-03T05:46:14.814477Z",
    "text_preview": "Furthermore, it is important to note that AI plays a crucial role...",
    "attribution": "AI-generated",
    "label_variant": "uncertain",
    "signals": {
      "linguistic_score": 0.7044,
      "llm_score": 0.8,
      "combined_score": 0.7618,
      "confidence": 0.4735
    },
    "status": "under_review",
    "appeal_id": "4074ebe6-1d29-4b7a-afe6-4e947c0752a9"
  },
  {
    "event": "appeal",
    "appeal_id": "4074ebe6-1d29-4b7a-afe6-4e947c0752a9",
    "content_id": "9b3cb643-d597-48d0-b736-f3dde8e34f5b",
    "timestamp": "2026-07-03T05:46:17.269987Z",
    "original_attribution": "AI-generated",
    "original_signals": {
      "linguistic_score": 0.7044,
      "llm_score": 0.8,
      "combined_score": 0.7618,
      "confidence": 0.4735
    },
    "original_label_variant": "uncertain",
    "creator_reasoning": "I wrote this myself as a summary paragraph for a policy brief. The formal register and transition phrases are standard in my field.",
    "status": "pending_review"
  }
]
```

---

## Appeals Workflow

Any creator with a `content_id` from a `POST /submit` response can file an appeal.

**Request:** `POST /appeal/<content_id>` with body `{ "creator_reasoning": "..." }`
(minimum 20 characters).

**What happens:**
1. System looks up the `content_id` in `audit_log.json` — returns 404 if not found
2. Returns 409 if an appeal is already pending
3. Mutates the original classification entry: `status` → `"under_review"`, adds `appeal_id`
4. Appends a new `appeal` event with the creator's verbatim reasoning and the original signal scores
5. Returns a confirmation with `appeal_id` and the original classification details

A human moderator reviews both records side by side via `GET /log` or
`GET /status/<content_id>`. The system does not auto-resolve appeals.

---

## Known Limitations

**Non-native English writers using learned formal register.** Writers whose first
language is not English are often taught explicit structural patterns in ESL
instruction — topic sentences, numbered transitions ("First... Second... In
conclusion..."), and consistent paragraph structure. These patterns score identically
to AI output on Signal 1 (transition-phrase density, consistent sentence lengths,
formal vocabulary). Signal 2 may also penalize the absence of idiomatic English
constructions. This population is systematically disadvantaged by both signals and
will receive `uncertain` or occasionally `high_confidence_ai` labels even for
entirely human-written work. Their best recourse is the appeals endpoint, where a
human moderator can recognize the register mismatch.

**Poets using deliberate anaphora and repetition.** A human poet writing in the
tradition of Walt Whitman — heavy anaphoric repetition, simple vocabulary, structurally
repeated clauses — will produce very low TTR scores and uniform sentence lengths.
Signal 1 will score this as AI-like. Signal 2 may partially compensate if it detects
the personal voice, but the combined result will typically land in `uncertain`.

---

## Spec Reflection

**One way the spec helped:** The explicit asymmetry requirement — "false positives
(labeling human work as AI) are worse than false negatives" — directly shaped the
confidence threshold decision. Without that framing, the natural instinct would have
been to set the threshold at 0.50 (majority vote). The spec's asymmetry requirement
pushed the threshold to 0.65 confidence *and* required a strong combined score
simultaneously. This double-gate is what prevents the academic-writing false positive
from firing a `high_confidence_ai` label even when both signals agree the text looks
AI-like.

**One way implementation diverged from the spec:** The spec described attribution as
a binary result. The implementation returns both `attribution` (a plain-English
`"Human-written"` or `"AI-generated"` string) *and* `ai_probability` (the raw
continuous combined score) in the response. A binary label alone would have been
misleading — a piece with combined_score 0.51 and confidence 0.02 produces
`"AI-generated"` but the system has almost no certainty. Surfacing the continuous
score allows callers to display more nuanced UI (e.g., a confidence bar) and gives
moderators a clearer picture than the binary string alone.

---

## AI Usage

**Instance 1 — Signal 1 function and Flask skeleton (M3)**

Directed the AI tool to generate the complete `linguistic_signal()` function and
the `POST /submit` endpoint skeleton, providing the Signal 1 specification from
`planning.md` (the four sub-features, their weights, and the output format) and the
Architecture Flow 1 diagram as context.

*What was revised:* The initial burstiness anchor was set at CV > 0.40 as the
human-writing threshold. After spot-checking the function against three test inputs,
the human diary-entry sample had CV = 0.63 (correctly identified as varied) but the
ramen-review sample had CV = 0.03 (only two sentences — too short for the metric to
be reliable). The anchor was adjusted to 0.55 and the sentence-count guard was added
after this test revealed the short-text edge case.

**Instance 2 — LLM classifier prompt (M4)**

Directed the AI tool to write the Groq prompt for `llm_signal()`, providing the
Signal 2 specification and the uncertainty representation section from `planning.md`.

*What was overridden:* The initial prompt produced a score of 0.40 for a clearly
human casual ramen review and called it "formulaic structure" — the model was treating
informal grammar and missing punctuation as AI signals rather than human ones. The
prompt was substantially revised to add a `CRITICAL` instruction explicitly stating
that informal, colloquial, or grammatically loose writing is a strong *human* signal,
followed by five concrete human-writing examples (the cat/Mochi story, the sourdough
starter, the victorian orphan landlord message, etc.) with explicit human-signal and
AI-signal checklists. After this revision, the clearly human sample dropped from
0.40 to 0.20 and confidence rose from 0.305 to 0.611.

---

## Setup

```bash
cp .env.example .env
# Add your GROQ_API_KEY to .env

pip install -r requirements.txt
python app.py
# Server runs on http://localhost:5001
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | API key from console.groq.com |

> **macOS note:** Port 5000 is reserved by AirPlay Receiver. The server runs on
> port 5001 by default.

---

## API Reference

### `POST /submit`
Submit text for attribution analysis.

**Request body:**
```json
{ "text": "Your text here (50–10,000 characters)", "creator_id": "user-123" }
```

**Response:**
```json
{
  "content_id": "uuid",
  "creator_id": "user-123",
  "timestamp": "ISO 8601 UTC",
  "attribution": "Human-written | AI-generated",
  "confidence": 0.0,
  "label": "Full label text shown to users",
  "ai_probability": 0.0,
  "transparency_label": {
    "variant": "high_confidence_human | high_confidence_ai | uncertain",
    "text": "Full label text shown to users"
  },
  "signals": {
    "linguistic": { "score": 0.0, "weight": 0.4, "features": {} },
    "llm":        { "score": 0.0, "weight": 0.6, "key_signals": [] }
  },
  "status": "classified"
}
```

**Rate limit:** 10/min, 50/hr, 200/day per IP.

---

### `POST /appeal/<content_id>`
Appeal a classification decision.

**Request body:**
```json
{ "creator_reasoning": "Explanation of why the classification is wrong (min 20 chars)" }
```

**Rate limit:** 5/hr per IP.

**Error codes:** 400 (missing/short reasoning), 404 (content_id not found),
409 (appeal already pending), 429 (rate limited).

---

### `GET /log`
Retrieve the full audit log as JSON.

**Response:** `{ "entries": [ ...classification and appeal records... ] }`

---

### `GET /status/<content_id>`
Check classification status and any filed appeals for a specific submission.

**Response:** `{ "content_id", "current_status", "classification", "appeals" }`
