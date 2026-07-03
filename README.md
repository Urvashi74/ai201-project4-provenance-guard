# Provenance Guard

A backend service that any creative sharing platform can plug into to classify
submitted text content, score confidence in that classification, surface a
transparency label to users, and handle appeals from creators who believe they've
been misclassified.

---

## How a Piece of Text Becomes a Label

This is the exact path a submission takes through the system, component by component.

**1. Rate Limiter**
Before any analysis runs, Flask-Limiter checks the requester's IP address against
per-IP counters stored in memory. If the caller has sent more than 10 requests in
the last minute (or 50 in the last hour), the request is rejected with HTTP 429 and
never reaches the detection pipeline. This gate protects the Groq API from abuse and
keeps latency predictable for legitimate creators.

**2. Input Validator**
The submission handler reads the JSON body and enforces two rules: the `content`
field must be present, and the text must be between 50 and 10,000 characters. Texts
shorter than 50 characters give the linguistic signal too little data to work with;
texts longer than 10,000 characters would push the Groq prompt past its useful
context window. Failures return HTTP 400 with a plain-English message.

**3. Signal 1 — Linguistic / Statistical Analyzer**
The first signal never calls an external API. It runs four statistical measurements
directly on the submitted text:

- **Sentence-length burstiness**: Humans naturally mix short punchy sentences with
  long complex ones (high coefficient of variation). AI-generated text tends to be
  metronomic — sentences cluster around a consistent length. A low CV score raises
  the AI-probability estimate.

- **Vocabulary richness (type-token ratio)**: The ratio of unique words to total
  words, adjusted logarithmically for text length. AI text repeats concepts and
  relies on a narrower effective vocabulary than human writing at the same length.
  A low TTR raises the AI-probability estimate.

- **AI transition-phrase density**: A curated list of phrases that LLMs
  statistically overuse — "furthermore", "it is worth noting", "delve into",
  "plays a crucial role", and ~20 others. Each hit increments a counter; the
  counter is normalized to a 0–1 score. Multiple hits strongly raise the estimate.

- **Average word length**: AI output skews toward formal, polysyllabic diction.
  A higher average word length nudges the estimate upward.

The four sub-scores are combined with fixed weights (burstiness 35%, TTR 30%,
transitions 25%, word length 10%) into a single `linguistic_score` in [0, 1].

**4. Signal 2 — LLM Classifier (Groq)**
The second signal calls the Groq API with `llama-3.1-8b-instant`. A structured
prompt asks the model to act as a forensic linguist and return a JSON object with an
`ai_probability` float and the key signals it observed. The model runs at temperature
0.1 to minimize stochastic variance. If the API call fails, this signal falls back
gracefully to 0.5 (maximum uncertainty) and logs the error — the endpoint never
returns a 500 to the caller.

**5. Confidence Scorer**
The two signal scores are fed into the confidence scorer, which does two things:

First, it computes a `combined_score` as a weighted average:

```
combined = 0.40 × linguistic_score + 0.60 × llm_score
```

The LLM signal is weighted higher because it reads full semantic context, not just
surface statistics.

Second, it computes a `confidence` value:

```
confidence = |combined − 0.5| × 2 × (1 − |linguistic − llm|)
```

The first factor measures how far the combined score is from center (0.5 = pure
uncertainty). The second factor penalizes disagreement between the two signals. If
one signal says 0.8 and the other says 0.2, confidence collapses toward zero even
though the average is 0.5 — the system is honest about its uncertainty.

A `combined_score` of 0.51 produces a confidence near 0.02. A `combined_score` of
0.95 with both signals in strong agreement produces a confidence near 0.90. These
two cases produce meaningfully different labels.

**6. Label Generator**
Takes `combined_score` and `confidence` and selects one of three transparency label
variants. Thresholds are intentionally conservative: high-confidence labels require
both a strong score *and* high inter-signal agreement. This reflects the spec's
asymmetry: mislabeling a human creator's work as AI is worse than missing an AI
piece, so the system defaults to "uncertain" in ambiguous cases.

**7. Audit Logger**
Every classification is immediately appended to `audit_log.json` as a structured
JSON record: content ID, timestamp, attribution, both signal scores, combined score,
confidence, label variant, and status. Only the first 120 characters of the text are
stored as a preview. The log is also updated on appeal: the original entry's status
changes to `under_review`, and a second record captures the creator's reasoning.

**8. Response**
The endpoint returns a single JSON object: `content_id`, `timestamp`, `attribution`,
`ai_probability`, `confidence_score`, `transparency_label` (variant + full text), and
a `signals` object with both raw scores and the features each detector found.

---

## Transparency Label Variants

These are the exact verbatim strings the system surfaces to users.

### `high_confidence_ai`

Triggered when `combined_score >= 0.70` AND `confidence >= 0.65`.

> AI-Generated Content: Our analysis strongly indicates this content was produced by
> an AI writing system. Multiple signals — including structural patterns, vocabulary
> characteristics, and stylistic markers — point to AI authorship. If you wrote this
> yourself and believe this assessment is wrong, you can submit an appeal and a human
> moderator will review your case.

### `high_confidence_human`

Triggered when `combined_score <= 0.30` AND `confidence >= 0.65`.

> Human-Created Content: Our analysis strongly indicates this content was written by
> a human. The writing shows characteristics consistent with authentic human
> authorship: varied sentence rhythm, personal voice, and idiosyncratic word choices.
> No significant AI-generation signals were detected.

### `uncertain`

Triggered in all other cases, including when signals disagree regardless of the
combined score.

> Origin Uncertain: We could not determine with confidence whether this content was
> written by a human or generated by AI. It may be entirely human-written,
> AI-generated, or a blend of both. Our detection signals gave mixed or weak results.
> Readers should draw their own conclusions, and creators who feel this is inaccurate
> are welcome to submit an appeal.

---

## Rate Limiting

Rate limiting is applied per IP address using Flask-Limiter with in-memory storage.

| Endpoint | Limit | Reasoning |
|---|---|---|
| `POST /submit` | 10/min, 50/hr, 200/day | A legitimate creator won't submit 10 pieces in a minute. The burst limit allows rapid draft iteration; the hourly/daily caps prevent sustained API abuse. |
| `POST /appeal/<id>` | 5/hr | Appeals require human reasoning and are inherently low-frequency. This cap prevents appeal flooding without burdening creators contesting several pieces. |

If a limit is exceeded, the API returns HTTP 429 with a JSON error body. The limit
rationale deliberately accepts the trade-off that a single user behind a shared IP
(e.g., a school network) may hit limits sooner — this is acceptable given that the
primary threat is automated scraping, not human creators.

---

## Multi-Signal Detection Pipeline

Two independent signals are computed and combined.

### Signal 1: Linguistic / Statistical Analysis
- **Input**: raw text
- **Output**: `linguistic_score` in [0, 1] (0 = human, 1 = AI)
- **Sub-features**: sentence-length CV, type-token ratio, AI transition phrase count, average word length
- **No external calls** — deterministic and fast

### Signal 2: Groq LLM Classifier
- **Input**: raw text (up to 2,500 chars)
- **Output**: `llm_score` in [0, 1] + list of observed key signals
- **Model**: `llama-3.1-8b-instant` via Groq API, temperature 0.1
- **Fallback**: returns 0.5 if API call fails

### Combination
```
combined_score = 0.40 x linguistic_score + 0.60 x llm_score
confidence     = |combined_score - 0.5| x 2 x (1 - |linguistic_score - llm_score|)
```

---

## Confidence Scoring

Confidence is not the same as the AI-probability estimate. It measures *certainty*.

| Scenario | combined_score | Disagreement | confidence | Label |
|---|---|---|---|---|
| Both signals weakly agree: AI | 0.51 | 0.02 | ~0.02 | `uncertain` |
| Signals split | 0.50 | 0.80 | ~0.00 | `uncertain` |
| Both signals moderately agree: AI | 0.72 | 0.10 | ~0.62 | `high_confidence_ai` |
| Both signals strongly agree: AI | 0.92 | 0.05 | ~0.85 | `high_confidence_ai` |
| Both signals strongly agree: human | 0.08 | 0.05 | ~0.85 | `high_confidence_human` |

---

## Appeals Workflow

Creators who dispute a classification send `POST /appeal/<content_id>` with a JSON
body containing a `reasoning` field (minimum 20 characters).

The system:
1. Looks up the original classification by `content_id` in the audit log
2. Returns HTTP 404 if no classification exists, HTTP 409 if an appeal is already pending
3. Updates the original log entry's `status` from `classified` to `under_review`
4. Appends a new `appeal` event to the audit log containing: `appeal_id`, `content_id`,
   timestamp, original attribution, original confidence, and the creator's verbatim reasoning
5. Returns a confirmation with the appeal ID and original classification details

The appeal is logged alongside the original decision. A human moderator reviews both
together. The system does not auto-resolve appeals.

---

## Audit Log

Every attribution decision and every appeal is written to `audit_log.json`. The log
is append-only at runtime; entries are never deleted or overwritten (status fields on
existing entries are mutated on appeal).

Retrieve the log: `GET /log?limit=50&offset=0&event=classification`

### Sample entries (3 events)

```json
[
  {
    "event": "classification",
    "content_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "timestamp": "2026-07-01T14:22:10Z",
    "content_preview": "The morning light crept through the curtains like it owed me something. I hadn't slept...",
    "attribution": "Human-written",
    "ai_probability": 0.21,
    "confidence_score": 0.74,
    "label_variant": "high_confidence_human",
    "signals": {
      "linguistic_score": 0.18,
      "llm_score": 0.23
    },
    "status": "classified"
  },
  {
    "event": "classification",
    "content_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "timestamp": "2026-07-01T14:35:44Z",
    "content_preview": "In conclusion, it is important to note that artificial intelligence plays a crucial role in...",
    "attribution": "AI-generated",
    "ai_probability": 0.87,
    "confidence_score": 0.81,
    "label_variant": "high_confidence_ai",
    "signals": {
      "linguistic_score": 0.79,
      "llm_score": 0.91
    },
    "status": "under_review"
  },
  {
    "event": "appeal",
    "appeal_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
    "content_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "timestamp": "2026-07-01T14:41:02Z",
    "original_attribution": "AI-generated",
    "original_confidence": 0.81,
    "original_label_variant": "high_confidence_ai",
    "creator_reasoning": "I wrote this myself as part of my thesis on urban planning. The formal tone is intentional — it's an academic excerpt, not creative writing.",
    "status": "pending_review"
  }
]
```

After the appeal in entry 3, the classification entry for `b2c3d4e5...` has its
`status` field updated to `"under_review"` in the log.

---

## Setup

```bash
cp .env.example .env
# Add your GROQ_API_KEY to .env

pip install -r requirements.txt
python app.py
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | API key from console.groq.com |

---

## API Reference

### `POST /submit`
Submit text content for attribution analysis.

**Request body:**
```json
{ "content": "Your text here (50-10,000 characters)" }
```

**Response:**
```json
{
  "content_id": "uuid",
  "timestamp": "ISO 8601",
  "attribution": "Human-written | AI-generated",
  "ai_probability": 0.0,
  "confidence_score": 0.0,
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
{ "reasoning": "Explanation of why the classification is wrong (min 20 chars)" }
```

**Rate limit:** 5/hr per IP.

---

### `GET /log`
Retrieve the audit log.

**Query params:** `limit` (default 50), `offset` (default 0), `event` (`classification` | `appeal`)

---

### `GET /status/<content_id>`
Check the current status of a classified piece and any associated appeals.
