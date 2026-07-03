# Provenance Guard — Planning

## Required Deliverables Checklist

- [x] Content Submission Endpoint (`POST /submit`) — returns attribution, confidence score, label text
- [x] Multi-Signal Detection Pipeline — at least 2 distinct signals (documented below)
- [x] Confidence Scoring with Uncertainty — 0.51 and 0.95 produce meaningfully different labels
- [x] Transparency Label — 3 verbatim variants in README
- [x] Appeals Workflow — captures reasoning, logs it, updates status to "under review"
- [x] Rate Limiting — on submission endpoint, reasoning documented
- [x] Audit Log — every attribution decision logged; 3+ entries visible via `GET /log`
- [x] Architecture diagram — see below

---

## Design Decisions

### 1. Detection Signals

**Signal 1: Linguistic / Statistical Analysis**
Output: a single float score in [0, 1] where 0 = likely human, 1 = likely AI.
Computed from four sub-scores with fixed weights:

| Sub-feature | Weight | What it measures |
|---|---|---|
| Sentence-length burstiness (CV) | 35% | Variability of sentence lengths — humans vary more |
| Vocabulary richness (TTR) | 30% | Unique-word ratio adjusted for length — AI repeats more |
| AI transition-phrase density | 25% | Count of overused LLM phrases, normalized to [0,1] |
| Average word length | 10% | Proxy for formal/academic diction |

`linguistic_score = 0.35 × burstiness_ai + 0.30 × ttr_ai + 0.25 × transitions + 0.10 × word_len_ai`

**Signal 2: LLM Classifier (Groq / llama-3.1-8b-instant)**
Output: a single float `ai_probability` in [0, 1] plus a list of observed key signals
(strings). The model is prompted to evaluate personal voice, lived-experience
specificity, structural formulaism, and generic vs. idiosyncratic phrasing. Runs at
temperature 0.1. If the API call fails, falls back to 0.5 (maximum uncertainty).

**How the two scores are combined:**
```
combined_score = 0.40 × linguistic_score + 0.60 × llm_score
confidence     = |combined_score − 0.5| × 2 × (1 − |linguistic_score − llm_score|)
```

The LLM signal is weighted higher (60%) because it reads semantic meaning, not just
surface statistics. The `(1 − disagreement)` factor suppresses confidence when the
two signals contradict each other, making disagreement produce honest uncertainty
rather than a spurious average.

---

### 2. Uncertainty Representation

**What a specific confidence value means:**

| confidence value | Meaning | Label shown |
|---|---|---|
| 0.00 – 0.10 | Signals gave almost no information, or contradicted each other completely | `uncertain` |
| 0.10 – 0.40 | Weak lean in one direction; not enough to act on | `uncertain` |
| 0.40 – 0.64 | Moderate lean; signals mostly agree but score isn't extreme | `uncertain` |
| 0.65 – 0.80 | Strong lean with agreeing signals | `high_confidence_ai` or `high_confidence_human` |
| 0.80 – 1.00 | Both signals strongly agree and score is far from center | `high_confidence_ai` or `high_confidence_human` |

**What a confidence of 0.6 specifically means:**
The combined score is ~0.30 away from center (e.g., combined ≈ 0.80) and both
signals broadly agree (low disagreement). The system leans toward "AI-generated" but
falls just below the 0.65 threshold for a high-confidence label. It shows `uncertain`
because the risk of a false positive — publicly marking a human creator's work as AI
— outweighs the marginal clarity gained from a more definitive label at this
confidence level. Confidence 0.60 is "we lean toward this, but not strongly enough
to stake a claim."

**Exact thresholds separating the three regions:**

```
combined_score >= 0.70  AND  confidence >= 0.65  →  "Likely AI"    (high_confidence_ai)
combined_score <= 0.30  AND  confidence >= 0.65  →  "Likely Human" (high_confidence_human)
everything else                                  →  "Uncertain"    (uncertain)
```

The score threshold (0.70 / 0.30) and confidence threshold (0.65) are both required
simultaneously. A combined_score of 0.90 with confidence of 0.50 (because the two
signals disagree) still produces `uncertain`. This double-gate design means the
system must be both directionally clear *and* internally consistent to fire a
high-confidence label.

---

### 3. Transparency Label Text

These are the exact strings the system returns in `transparency_label.text`.

**`high_confidence_ai`** (combined ≥ 0.70 AND confidence ≥ 0.65):

> AI-Generated Content: Our analysis strongly indicates this content was produced by
> an AI writing system. Multiple signals — including structural patterns, vocabulary
> characteristics, and stylistic markers — point to AI authorship. If you wrote this
> yourself and believe this assessment is wrong, you can submit an appeal and a human
> moderator will review your case.

**`high_confidence_human`** (combined ≤ 0.30 AND confidence ≥ 0.65):

> Human-Created Content: Our analysis strongly indicates this content was written by
> a human. The writing shows characteristics consistent with authentic human
> authorship: varied sentence rhythm, personal voice, and idiosyncratic word choices.
> No significant AI-generation signals were detected.

**`uncertain`** (all other cases):

> Origin Uncertain: We could not determine with confidence whether this content was
> written by a human or generated by AI. It may be entirely human-written,
> AI-generated, or a blend of both. Our detection signals gave mixed or weak results.
> Readers should draw their own conclusions, and creators who feel this is inaccurate
> are welcome to submit an appeal.

---

### 4. Appeals Workflow

**Who can submit an appeal:**
Any caller who has a valid `content_id` from a previous `POST /submit` response.
The system does not authenticate identity — it trusts that the person submitting the
appeal is the creator, consistent with the platform's own auth layer handling that
concern. Rate limit: 5 appeals per hour per IP.

**What information they must provide:**
A `reasoning` field (string, minimum 20 characters) explaining why they believe the
classification is wrong. The system does not prescribe format; the field is free text.
A creator might say: "I wrote this over six months; the formal register is required
by my institution's style guide. My supervisor has supervised four drafts."

**What the system does when an appeal is received:**

1. Looks up `content_id` in `audit_log.json` — returns 404 if not found.
2. Checks that the entry's status is not already `"under_review"` — returns 409 if
   a duplicate appeal is attempted.
3. Validates the reasoning meets the 20-character minimum — returns 400 if not.
4. Mutates the original classification entry: sets `status = "under_review"` and
   adds `appeal_id` to that record.
5. Appends a new `appeal` event record to the log containing: `appeal_id`,
   `content_id`, timestamp, original attribution, original confidence score, original
   label variant, and the creator's verbatim reasoning.
6. Returns a 200 response with the `appeal_id`, confirmation message, and the
   original classification details so the creator knows exactly what they're contesting.

**What a human reviewer sees when they open the appeal queue:**

The reviewer calls `GET /log?event=appeal` and sees every appeal record. For each
appeal they can then call `GET /status/<content_id>` to see both the original
classification record and the appeal record side by side:

- Original: `attribution`, `ai_probability`, `confidence_score`, `label_variant`,
  both raw signal scores (`linguistic_score`, `llm_score`), and the text preview
- Appeal: the creator's verbatim `creator_reasoning` and the timestamp

This gives the reviewer everything needed to evaluate the decision: not just the
final label, but *why* each signal fired — so they can identify cases where high
signal scores were caused by genre or register (e.g., academic writing) rather than
actual AI generation.

---

### 5. Anticipated Edge Cases

These are specific content types the system will handle poorly. Not generic risks —
specific scenarios with specific failure modes.

**Edge Case 1: A poet who uses deliberate anaphora and repetition**

A human poet writes a piece in the tradition of Walt Whitman or Martin Luther King Jr.
— heavy anaphora ("I have watched the sun rise over...", "I have watched the children
play..."), simple vocabulary, and structurally repeated clauses. The type-token ratio
will be very low (the same root words repeat throughout). Sentence lengths will be
eerily consistent (each anaphoric unit is approximately the same length). Signal 1
will score this as highly AI-like. Signal 2 may partially compensate if it detects
the personal voice, but the combined score will push toward `uncertain` or possibly
even `high_confidence_ai` depending on how formulaic the anaphora appears to the LLM.

The correct label is `high_confidence_human`. The system will likely return `uncertain`
or worse. A human reviewer handling the poet's appeal will need to recognize that
repetition is a centuries-old poetic device, not an AI artifact.

**Edge Case 2: A non-native English speaker writing in a learned formal register**

A writer whose first language is not English often writes with more predictable
sentence structures (learned from grammar instruction), simpler and more consistent
vocabulary (drawing on a smaller active English vocabulary), and explicit transition
phrases ("First... Second... In conclusion...") because ESL instruction teaches these
as markers of organized writing. All four Signal 1 features will score this text as
AI-like. Signal 2 will likely find the absence of idiomatic English constructions
and the predictable structure "generic" — a false proxy for AI authorship. This
population of writers is systematically disadvantaged by both signals.

**Edge Case 3: Human-written content marketing copy (bonus)**

A brand copywriter produces consistent, voice-guided text: no first-person opinions,
structured lists, consistent diction, deliberate absence of personal quirks. This
is professional human writing designed to sound like a brand, not a person. Both
signals will score it as AI-generated, and they will be consistently wrong for this
entire class of submission. The `uncertain` label will fire when confidence is low
enough; but a well-trained copywriter whose work always scores near `high_confidence_ai`
has no good appeal argument because the system's reasoning ("no personal voice") is
technically accurate — they deliberately removed their personal voice.

---

## Detection Signal Analysis

For each signal: what property it measures, why that property differs between human
and AI writing, and what it cannot capture.

---

### Signal 1: Linguistic / Statistical Analysis

**What property it measures**

Four surface-level statistical properties of the text, combined with fixed weights:

1. *Sentence-length burstiness* — the coefficient of variation (CV) of sentence
   lengths. A high CV means the writer alternates freely between short and long
   sentences. A low CV means sentence lengths are uniform.

2. *Vocabulary richness (TTR)* — type-token ratio: unique words divided by total
   words, adjusted logarithmically for text length. Low TTR means the text repeats
   the same words and concepts.

3. *AI transition-phrase density* — count of phrases from a curated list that LLMs
   statistically overuse: "furthermore", "it is worth noting", "delve into", "plays
   a crucial role", and ~20 others. Normalized to [0, 1].

4. *Average word length* — a proxy for lexical formality. Longer average words
   suggest academic or formal diction.

Final score: `0.35 × burstiness + 0.30 × TTR + 0.25 × transitions + 0.10 × word_len`

**Why this property differs between human and AI writing**

AI language models generate text by predicting the next token, optimized for fluency
and coherence. This produces characteristic statistical signatures:

- Models have been trained on text humans labeled as clear and well-structured.
  "Clear" writing in training data tends to use moderate, consistent sentence lengths.
  Human writers have no such constraint — they write to express thought, not to
  optimize readability scores, so their rhythm is erratic.

- Models draw from large vocabularies but over-index on the most frequent words for
  a given topic. A model writing about "climate change" will reach for the same dozen
  topic words throughout; a human writer reaches for synonyms, metaphors, and
  tangents.

- The transition phrases are direct training artifacts. Models have processed millions
  of essays and articles that used "furthermore" as structural glue, so they reproduce
  it. These phrases appear in model output far more often than in natural human prose.

**What it cannot capture — blind spots**

- *Academic and formal human writing.* A PhD student writing a literature review,
  a lawyer drafting a brief, a journalist writing a structured feature — all of these
  will naturally use transition phrases, formal diction, and consistent sentence
  structure. On all four features, this signal will score them as AI-like. This is
  the primary false-positive risk.

- *Style mimicry in both directions.* A human deliberately writing in a casual,
  varied style scores as human. An AI prompted to "write like a stream-of-consciousness
  diary entry with short choppy sentences" can produce high burstiness and avoid
  transition phrases entirely, defeating this signal.

- *Genre effects.* Poetry has atypical sentence structure and unusual word choices
  that may score as human for the wrong reasons. Technical documentation may score
  as AI for the wrong reasons.

- *Meaning is invisible to this signal.* The signal does not know what the text is
  about. A human writing about machine learning uses formal vocabulary; this signal
  penalizes that. A human writing a personal essay about grief in plain language may
  score as human even if it has a high TTR by accident.

- *Short texts.* Under ~80 words, the CV and TTR measures are statistically unreliable.
  The 50-character minimum in the validator mitigates this but does not eliminate it.

---

### Signal 2: LLM Classifier (Groq / llama-3.1-8b-instant)

**What property it measures**

The semantic and stylistic coherence of the text as judged by a language model
prompted to act as a forensic linguist. The prompt directs it to evaluate:
- Whether the text has a distinct personal voice
- Whether it contains specific concrete detail suggesting lived experience
- Whether its structure is formulaic or organic
- Whether phrasing is generic vs. idiosyncratic

The model returns a single `ai_probability` float and a list of the key signals it
observed, at temperature 0.1 to minimize stochastic variance.

**Why this property differs between human and AI writing**

Human writing reflects a specific person with specific experiences. A human writing
about grief experienced something and chooses words that carry their particular weight
of that experience. AI writing about grief draws on the statistical distribution of
how grief has been described across millions of texts — it produces plausible,
empathic-sounding sentences without the specific concrete detail that only someone
with that lived experience would include.

"My mother used to leave folded notes inside my lunch box, always the same blue ink"
is the kind of detail human writers include because it's true. AI writers typically
don't, because it serves no general communicative purpose. The classifier model is
sensitive to the difference between specificity and generality in a way that
surface statistics cannot capture.

**What it cannot capture — blind spots**

- *AI text prompted toward specificity.* An AI instructed to "include one concrete
  sensory memory and avoid transition phrases" produces text that looks very human to
  a classifier. As prompting techniques improve, this gap narrows. This signal will
  degrade over time.

- *Deliberately generic human writing.* A human writing product descriptions, template
  letters, or content intended for mass audiences intentionally strips out personal
  voice. This scores as AI. Human content writers will be systematically
  misclassified by this signal.

- *The classifier's own training bias.* We're asking an LLM to detect LLM output.
  The classifier shares training data biases with the text it evaluates. It may
  systematically miss patterns from models trained after its own knowledge cutoff,
  and it may penalize writing styles that were underrepresented in its training data
  (non-Western narrative conventions, oral storytelling traditions, etc.).

- *Short texts.* Under ~100 words the model has less signal. It doesn't always
  report lower confidence on short texts, so its scores at low word counts can be
  overconfident in either direction.

- *Non-English and code-switched text.* The model is primarily English-trained.
  Heavy dialect, bilingual code-switching, or non-English submissions will produce
  unreliable scores.

- *Temperature variance.* Even at temperature 0.1, two calls with the same text
  can return scores that differ by ~0.05–0.10. A text with a true AI probability
  near 0.5 can flip classification between calls.

---

## False Positive Scenario Trace

**Scenario: An academic writer submits a literature review excerpt.**

A PhD student submits 420 words from a chapter she wrote herself over several months.
The text uses formal diction, transition phrases ("furthermore", "it is essential to
note that"), consistent sentence lengths (~18 words each), and discipline-specific
vocabulary. She wrote every word; no AI tool was involved.

---

**Step 1 — Rate Limiter**
She's submitting for the first time today. Passes without issue.

**Step 2 — Input Validator**
420 words is well within the 50–10,000 character bounds. Passes.

**Step 3 — Linguistic Signal**
- Sentence CV: 0.21 (very uniform — all ~18-word sentences) → burstiness_score: 0.62
- TTR: technical vocabulary repeats key terms ("epistemological", "ontological")
  → ttr_score: 0.55
- Transition phrases: "furthermore" (×2), "it is essential to note" (×1), "moreover"
  (×1) → 4 hits → transition_score: 1.00 (capped)
- Avg word length: 7.9 chars (academic jargon) → word_len_score: 0.80

  `linguistic_score = 0.35×0.62 + 0.30×0.55 + 0.25×1.00 + 0.10×0.80`
  `               = 0.217 + 0.165 + 0.250 + 0.080 = 0.712`

**Step 4 — LLM Signal**
The classifier reads a well-structured, formally written literature review. No
concrete personal experience, no idiosyncratic voice, formulaic structure throughout.
It returns `ai_probability: 0.71`.

**Step 5 — Confidence Scorer**
```
combined  = 0.40 × 0.712 + 0.60 × 0.71  = 0.285 + 0.426 = 0.711
disagreement = |0.712 − 0.71| = 0.002  (signals strongly agree)
confidence = |0.711 − 0.5| × 2 × (1 − 0.002)
           = 0.422 × 0.998 ≈ 0.421
```

**Step 6 — Label Generator**
`combined_score = 0.711` and `confidence = 0.421`.
The `high_confidence_ai` threshold requires `confidence >= 0.65`. She's at 0.421.
**The system returns the `uncertain` label:**

> "Origin Uncertain: We could not determine with confidence whether this content was
> written by a human or generated by AI. It may be entirely human-written,
> AI-generated, or a blend of both..."

The system does not publicly accuse her of AI generation. This is the designed
behavior — the conservative confidence threshold is what protects her.

---

**What if both signals were even higher? (Worst-case false positive)**

If the text was even more formulaic (linguistic=0.88, llm=0.85):
```
combined  = 0.40×0.88 + 0.60×0.85 = 0.352 + 0.510 = 0.862
disagreement = |0.88 − 0.85| = 0.03
confidence = |0.862 − 0.5| × 2 × (1 − 0.03) = 0.724 × 0.97 ≈ 0.702
```
`confidence = 0.702 >= 0.65` → **`high_confidence_ai` label fires.** This is the
genuine false positive: she receives a label that publicly marks her work as AI.

**How she appeals:**

She sends `POST /appeal/<content_id>` with body:
```json
{
  "reasoning": "This is an excerpt from my PhD dissertation on post-structuralist
  epistemology, written over 18 months. The formal register and transition phrases
  are required by my institution's style guide. My supervisor has supervised this
  chapter through four drafts. I can provide the full manuscript."
}
```

The system immediately:
1. Finds her original classification record in `audit_log.json`
2. Sets its `status` field to `"under_review"`
3. Appends a new `appeal` event containing her verbatim reasoning alongside the
   original signal scores (linguistic=0.88, llm=0.85) and the confidence (0.702)
4. Returns her an `appeal_id` and confirms the review is pending

A human moderator opens `GET /log` and sees both records side by side. They can
observe that the signal scores were high *because* the text is academic — not because
it exhibits the creativity-absent, generic-phrasing hallmarks of actual AI output.
They overturn the decision.

---

## API Surface Contract

This is the endpoint contract — what each endpoint accepts and returns. Not code;
the contract that all code will implement.

---

### POST /submit

**Purpose:** Submit text for attribution analysis.

**Accepts:**
```
Content-Type: application/json
Body: { "content": string }
```
Constraints: `content` must be a string, 50–10,000 characters.
Rate limit: 10/min, 50/hr, 200/day per IP.

**Returns 200:**
```json
{
  "content_id": "uuid (caller stores this to file an appeal)",
  "timestamp":  "ISO 8601 UTC",
  "attribution": "Human-written | AI-generated",
  "ai_probability":   "float [0,1] — P(AI-generated)",
  "confidence_score": "float [0,1] — certainty in the attribution",
  "transparency_label": {
    "variant": "high_confidence_ai | high_confidence_human | uncertain",
    "text":    "verbatim string shown to platform users"
  },
  "signals": {
    "linguistic": {
      "score":    "float [0,1]",
      "weight":   0.40,
      "features": "{ burstiness_cv, ttr, ai_phrase_hits, avg_word_length, ... }"
    },
    "llm": {
      "score":       "float [0,1]",
      "weight":      0.60,
      "key_signals": ["observed signal 1", "observed signal 2", "..."]
    }
  },
  "status": "classified"
}
```

**Returns 400:** Missing or invalid `content` field.
**Returns 429:** Rate limit exceeded.

---

### POST /appeal/<content_id>

**Purpose:** Creator contests a classification they believe is wrong.

**Accepts:**
```
Content-Type: application/json
Body: { "reasoning": string }
```
Constraints: `reasoning` must be ≥ 20 characters. `content_id` must exist in the
audit log and must not already have status `under_review`.
Rate limit: 5/hr per IP.

**Returns 200:**
```json
{
  "appeal_id":   "uuid",
  "content_id":  "uuid (same as path param)",
  "status":      "appeal_submitted",
  "message":     "plain-English confirmation for the creator",
  "original_classification": {
    "attribution":     "Human-written | AI-generated",
    "confidence_score": "float",
    "label_variant":    "string"
  },
  "your_reasoning": "verbatim string the creator submitted",
  "timestamp":      "ISO 8601 UTC"
}
```

**Returns 400:** Missing or too-short `reasoning`.
**Returns 404:** `content_id` not found in audit log.
**Returns 409:** Appeal already pending for this content.
**Returns 429:** Rate limit exceeded.

---

### GET /log

**Purpose:** Retrieve the structured audit log for moderation or audit.

**Accepts:**
```
Query params:
  limit  — int, default 50, max 200
  offset — int, default 0
  event  — "classification" | "appeal" (optional filter)
```

**Returns 200:**
```json
{
  "total_entries": "int",
  "offset":        "int",
  "limit":         "int",
  "entries": [
    "array of classification and/or appeal event records"
  ]
}
```

---

### GET /status/<content_id>

**Purpose:** Check the current state of a specific submission and all its appeals.

**Returns 200:**
```json
{
  "content_id":      "uuid",
  "current_status":  "classified | under_review",
  "classification":  "the original classification log record",
  "appeals":         "array of appeal log records for this content_id"
}
```

**Returns 404:** `content_id` not found.

---

## Architecture Narrative

This is the path a single piece of text takes from submission to the label a user sees.

**1. Rate Limiter**
Before any analysis runs, Flask-Limiter checks the requester's IP address against
per-IP counters stored in memory. If the caller has sent more than 10 requests in
the last minute, or 50 in the last hour, the request is rejected with HTTP 429 and
never touches the detection pipeline. This gate protects the Groq API from abuse and
keeps latency predictable for legitimate creators.

**2. Input Validator**
The submission handler reads the JSON body and enforces two rules: the `content`
field must be present, and the text must be between 50 and 10,000 characters. Texts
shorter than 50 characters give the linguistic signal too little data to work with;
texts longer than 10,000 characters would push the Groq prompt past its useful
context window. Failures return HTTP 400 with a plain-English message.

**3. Signal 1 — Linguistic / Statistical Analyzer**
The first signal never calls an external API. It runs four statistical measurements
directly on the submitted text and combines them into a single `linguistic_score`.

**4. Signal 2 — LLM Classifier (Groq)**
The second signal calls the Groq API with `llama-3.1-8b-instant`. A structured
prompt asks the model to act as a forensic linguist and return `ai_probability` and
the key signals it observed. Falls back to 0.5 if the API call fails.

**5. Confidence Scorer**
Combines signals into a weighted `combined_score` (LLM 60%, linguistic 40%) and
computes `confidence` by penalizing disagreement between the two signals. A score
of 0.51 yields confidence ~0.02; a score of 0.95 with agreeing signals yields ~0.90.

**6. Label Generator**
Selects one of three variants. Requires `confidence >= 0.65` for any high-confidence
label, defaulting to `uncertain` in all ambiguous cases to minimize false positives.

**7. Audit Logger**
Appends a classification record to `audit_log.json`. Stores only the first 120
characters of the text as a preview. Updated again when an appeal arrives.

**8. Response**
Returns the full JSON object to the caller including all signal detail.

---

## Architecture

### Flow 1: Submission (POST /submit)

```
Client
  │
  │  POST /submit
  │  Body: { "content": "raw text string..." }
  │
  ▼
┌─────────────────────────────┐
│  Rate Limiter               │
│  (Flask-Limiter, per-IP)    │── 429 if over limit ──► Client
│  10/min · 50/hr · 200/day   │
└──────────────┬──────────────┘
               │  raw text (if within limits)
               ▼
┌─────────────────────────────┐
│  Input Validator            │
│  50–10,000 char check       │── 400 if invalid ──────► Client
└──────┬───────────────┬──────┘
       │               │
       │  validated    │  validated
       │  text string  │  text string
       ▼               ▼
┌──────────────┐  ┌──────────────────────────────┐
│  Signal 1    │  │  Signal 2                    │
│  Linguistic  │  │  LLM Classifier              │
│  Analyzer    │  │  (Groq llama-3.1-8b-instant) │
│              │  │  prompt: forensic linguist   │
│ · burstiness │  │  temperature: 0.1            │
│ · vocab TTR  │  │  fallback: score=0.5         │
│ · AI phrases │  │                              │
│ · word len   │  └────────────────┬─────────────┘
└──────┬───────┘                   │
       │                           │
       │  linguistic_score         │  llm_score ∈ [0,1]
       │  ∈ [0,1]                  │  + key_signals list
       │  + feature breakdown      │
       └───────────┬───────────────┘
                   │  both scores
                   ▼
       ┌───────────────────────────────────────┐
       │  Confidence Scorer                    │
       │  combined = 0.4×ling + 0.6×llm        │
       │  confidence = |c−0.5|×2×(1−|ling−llm|)│
       └───────────────┬───────────────────────┘
                       │  combined_score ∈ [0,1]
                       │  confidence ∈ [0,1]
                       ▼
             ┌─────────────────────────┐
             │  Label Generator        │
             │  if c≥0.70 & conf≥0.65  │
             │    → high_confidence_ai │
             │  if c≤0.30 & conf≥0.65  │
             │    → high_confidence_   │
             │        human            │
             │  else → uncertain       │
             └───────────┬─────────────┘
                         │  variant string
                         │  + verbatim label text
                         ▼
               ┌─────────────────────┐
               │  Audit Logger       │
               │  (audit_log.json)   │
               │  appends:           │
               │  { event:           │
               │    "classification",│
               │    content_id,      │
               │    scores,          │
               │    label_variant,   │
               │    status:          │
               │    "classified" }   │
               └───────────┬─────────┘
                           │  full response object
                           ▼
                        Client
                 { content_id, attribution,
                   ai_probability,
                   confidence_score,
                   transparency_label: { variant, text },
                   signals: { linguistic, llm },
                   status: "classified" }
```

---

### Flow 2: Appeal (POST /appeal/<content_id>)

```
Client
  │
  │  POST /appeal/<content_id>
  │  Body: { "reasoning": "creator explanation..." }
  │
  ▼
┌─────────────────────────────┐
│  Rate Limiter               │
│  (Flask-Limiter, per-IP)    │── 429 if over limit ──► Client
│  5/hr                       │
└──────────────┬──────────────┘
               │  content_id + reasoning string
               ▼
┌─────────────────────────────────────────────────┐
│  Appeal Handler                                 │
│                                                 │
│  1. Lookup content_id in audit_log.json         │── 404 if not found ──► Client
│  2. Check status != "under_review"              │── 409 if duplicate ──► Client
│  3. Validate len(reasoning) >= 20               │── 400 if too short ──► Client
│  4. Assign new appeal_id (UUID)                 │
└──────────────────┬──────────────────────────────┘
                   │  original log entry (index)
                   │  + validated reasoning
                   │  + new appeal_id
                   ▼
     ┌─────────────────────────────────┐
     │  Status Updater                 │
     │  audit_log[original_idx]        │
     │    .status = "under_review"     │
     │    .appeal_id = appeal_id       │
     └─────────────────┬───────────────┘
                       │  mutated log + new appeal event record
                       ▼
             ┌─────────────────────────┐
             │  Audit Logger           │
             │  (audit_log.json)       │
             │  appends:               │
             │  { event: "appeal",     │
             │    appeal_id,           │
             │    content_id,          │
             │    original_attribution,│
             │    original_confidence, │
             │    creator_reasoning,   │
             │    status:              │
             │    "pending_review" }   │
             └───────────┬─────────────┘
                         │  confirmation object
                         ▼
                      Client
               { appeal_id, content_id,
                 status: "appeal_submitted",
                 message: "...",
                 original_classification,
                 your_reasoning,
                 timestamp }
```

---

## Signal Design Rationale

**Why two signals?**
Neither signal is sufficient alone. The linguistic signal is fast and deterministic
but is blind to meaning — it penalizes academic human writers. The LLM signal reads
semantic context but can be fooled by targeted prompting and degrades as AI improves.
Combining them with a disagreement penalty means the system becomes *less* confident
when signals conflict — the honest behavior when evidence is ambiguous.

**Why weight LLM at 60%?**
The LLM reads actual meaning: personal voice, lived experience, generic vs. specific
phrasing. Statistics miss all of this. The linguistic signal is a strong corroborating
witness, not the primary one.

**Why is the confidence formula not just |combined − 0.5|?**
Because two signals that wildly disagree (linguistic=0.9, LLM=0.1) averaging to 0.5
should not produce confidence=0 only because the average is centered. Even at
combined=0.7, if signals disagree by 0.4, that disagreement is meaningful evidence
of model uncertainty. The `(1 − |Δ|)` factor captures this.

---

## AI Tool Plan

### M3 — Submission Endpoint + Signal 1

**Spec sections to provide:**
- Detection Signal Analysis → Signal 1 (Linguistic / Statistical Analysis): the four
  sub-features, their weights, and the output format (float in [0, 1])
- Architecture Flow 1 diagram (submission path), so the AI tool understands where
  Signal 1 sits and what it receives as input and emits as output
- API Surface Contract → `POST /submit` (accepted body, constraints, response shape)

**What to ask the AI tool to generate:**
1. A Flask app skeleton with `POST /submit` wired to a stub that calls the signal
   function and returns a placeholder JSON response in the correct shape
2. The complete `linguistic_signal(text: str) -> tuple[float, dict]` function
   implementing all four sub-features with the documented weights

**How to verify the output before wiring anything together:**
Call `linguistic_signal()` directly in a Python REPL with three test inputs:
- A clearly human piece (a personal diary entry or casual blog post) — expect score < 0.4
- A clearly AI piece (formal essay with transition phrases) — expect score > 0.6
- A borderline piece (academic writing) — expect score in 0.5–0.75 range

Check that the returned `features` dict contains all four sub-scores and that the
weighted combination math is correct by hand-verifying one example. Only wire into
the endpoint once the function passes all three spot checks.

---

### M4 — Signal 2 + Confidence Scoring

**Spec sections to provide:**
- Detection Signal Analysis → Signal 2 (LLM Classifier): what the model evaluates,
  the prompt structure, temperature setting, output format, and fallback behavior
- Design Decisions → Uncertainty Representation: the combination formula, the
  confidence formula, the threshold table, and the explicit meaning of confidence 0.6
- Architecture Flow 1 diagram, specifically the section from Signal 2 output through
  Confidence Scorer

**What to ask the AI tool to generate:**
1. The complete `llm_signal(text: str) -> tuple[float, list[str]]` function including
   the Groq API call, JSON response parsing, and the 0.5-fallback error handler
2. The `compute_confidence(linguistic_score, llm_score) -> tuple[float, float]`
   function that returns `(combined_score, confidence)` using the documented formulas

**What to check before integrating:**
Run both functions on the same three test inputs from M3, then verify the combined
behavior:
- Clearly human text: both signal scores should be < 0.5, confidence should be
  moderate-to-high (signals agree), combined should stay below 0.30
- Clearly AI text: both scores > 0.6, combined > 0.65, confidence > 0.55
- Borderline/academic text: linguistic high, LLM moderate → disagreement suppresses
  confidence, combined lands in uncertain zone

Specifically test the disagreement penalty: craft a case where `linguistic_score`
is 0.8 and mock `llm_score` to 0.2 and confirm `confidence` collapses toward 0
even though `combined` ≈ 0.44. This validates the `(1 − |Δ|)` factor is working.

---

### M5 — Production Layer (Labels + Appeal Endpoint)

**Spec sections to provide:**
- Design Decisions → Transparency Label Text: all three verbatim label strings and
  the exact threshold conditions that trigger each variant
- Design Decisions → Appeals Workflow: who can appeal, what they provide, every
  status change, and what a reviewer sees
- Architecture Flow 2 diagram (appeal path) with labeled arrows
- API Surface Contract → `POST /appeal/<content_id>` and `GET /log`

**What to ask the AI tool to generate:**
1. The `get_transparency_label(combined_score, confidence) -> tuple[str, str]`
   function with the exact verbatim label strings and documented thresholds
2. The complete `POST /appeal/<content_id>` endpoint: lookup, 404/409 guards,
   status mutation, audit log append, and response body
3. The `GET /log` endpoint with `limit`, `offset`, and `event` filter params

**How to verify all three label variants are reachable and the appeal flow works:**

Label coverage test — submit three crafted inputs and confirm each variant fires:
- `high_confidence_ai`: submit the transition-phrase-heavy AI text from M4 testing
  and confirm `label_variant == "high_confidence_ai"`
- `high_confidence_human`: submit a clearly personal, informal diary-style text and
  confirm `label_variant == "high_confidence_human"`
- `uncertain`: submit the academic writing sample and confirm `label_variant == "uncertain"`

Appeal flow test — end-to-end in three steps:
1. Submit any text via `POST /submit`, capture the `content_id`
2. Send `POST /appeal/<content_id>` with a reasoning string ≥ 20 characters; confirm
   the response contains `appeal_id` and `status: "appeal_submitted"`
3. Call `GET /status/<content_id>` and confirm `current_status == "under_review"` and
   the `appeals` array contains the reasoning submitted in step 2

Also verify the guard conditions: submit a second appeal to the same `content_id`
and confirm HTTP 409; submit to a nonexistent ID and confirm HTTP 404.

---

## Rate Limiting Rationale

`POST /submit`: **10 per minute, 50 per hour, 200 per day** per IP.

A human creator submitting a poem or short story is unlikely to submit more than a
few pieces in a minute. The 10/min limit allows a burst for a creator iterating on
drafts, while preventing automated scraping of the Groq API at hundreds of calls per
minute. The 50/hr and 200/day caps prevent sustained abuse while leaving headroom for
any real user.

`POST /appeal/<id>`: **5 per hour** per IP.

Appeals require human reasoning and are inherently low-frequency. Limiting to 5/hr
prevents flooding without burdening creators who need to contest several pieces.
