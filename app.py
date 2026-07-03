import json
import math
import os
import re
import string
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq

load_dotenv()

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

app = Flask(__name__)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[],
)

AUDIT_LOG_FILE = "audit_log.json"

# Phrases that LLMs statistically overuse — used by Signal 1
AI_TRANSITION_PHRASES = [
    "furthermore", "moreover", "additionally", "in conclusion",
    "it is important to note", "it is worth noting", "overall",
    "in summary", "delve into", "dive into", "in the realm of",
    "when it comes to", "having said that", "with that being said",
    "at the end of the day", "it goes without saying", "needless to say",
    "as an ai", "as a language model", "it's important to remember",
    "i must emphasize", "first and foremost", "last but not least",
    "in today's world", "in today's fast-paced", "in this day and age",
    "plays a crucial role", "plays an important role", "it is essential",
    "allows us to", "enables us to",
]


# ── Audit log helpers ─────────────────────────────────────────────────────────

def load_audit_log():
    if os.path.exists(AUDIT_LOG_FILE):
        with open(AUDIT_LOG_FILE, "r") as f:
            return json.load(f)
    return []


def save_audit_log(log):
    with open(AUDIT_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


# ── Signal 1: Linguistic / Statistical Analysis (M3 — fully implemented) ──────

def linguistic_signal(text: str) -> tuple[float, dict]:
    """
    Estimates AI probability from four statistical properties of the text.

    Returns (score, features) where score is a float in [0, 1]:
      0.0 → strong evidence of human writing
      1.0 → strong evidence of AI generation

    Sub-features and weights (per planning.md Signal 1 spec):
      burstiness (CV of sentence lengths)  35%
      vocabulary richness (TTR)            30%
      AI transition-phrase density         25%
      average word length                  10%
    """
    # Split into sentences and words
    sentences = [s.strip() for s in re.split(r"[.!?]+", text.strip()) if s.strip()]
    words = text.lower().split()
    clean_words = [w.strip(string.punctuation) for w in words if w.strip(string.punctuation)]

    if len(sentences) < 2 or len(clean_words) < 20:
        return 0.5, {"note": "text too short for reliable linguistic analysis"}

    # ── Sub-feature 1: Sentence-length burstiness ─────────────────────────────
    # Coefficient of variation (CV) of sentence lengths.
    # High CV → varied lengths → human-like → lower AI score.
    # Low CV  → metronomic lengths → AI-like → higher AI score.
    # Empirical anchor: CV > 0.55 is typical human prose; CV < 0.25 is typical AI.
    sent_lens = [len(s.split()) for s in sentences]
    mean_len = sum(sent_lens) / len(sent_lens)
    variance = sum((l - mean_len) ** 2 for l in sent_lens) / len(sent_lens)
    cv = math.sqrt(variance) / mean_len if mean_len > 0 else 0
    burstiness_ai_score = max(0.0, 1.0 - (cv / 0.55))

    # ── Sub-feature 2: Vocabulary richness (TTR) ──────────────────────────────
    # Type-token ratio adjusted logarithmically for text length.
    # Low adj-TTR → repetitive vocabulary → AI-like → higher AI score.
    ttr = len(set(clean_words)) / len(clean_words)
    length_adj_ttr = ttr * math.log10(len(clean_words) + 1)
    ttr_ai_score = max(0.0, 1.0 - (length_adj_ttr / 2.5))

    # ── Sub-feature 3: AI transition-phrase density ───────────────────────────
    # Counts hits from the curated phrase list, normalized to [0, 1] at 4+ hits.
    text_lower = text.lower()
    hit_count = sum(1 for phrase in AI_TRANSITION_PHRASES if phrase in text_lower)
    transition_ai_score = min(1.0, hit_count / 4.0)

    # ── Sub-feature 4: Average word length ───────────────────────────────────
    # Proxy for formal/academic diction. Normalized: 4 chars = 0, 8+ chars = 1.
    avg_wlen = sum(len(w) for w in clean_words) / len(clean_words)
    word_len_ai_score = max(0.0, min(1.0, (avg_wlen - 4.0) / 4.0))

    # ── Weighted combination (per planning.md spec) ───────────────────────────
    score = (
        0.35 * burstiness_ai_score
        + 0.30 * ttr_ai_score
        + 0.25 * transition_ai_score
        + 0.10 * word_len_ai_score
    )

    features = {
        "sentence_count": len(sentences),
        "word_count": len(clean_words),
        "burstiness_cv": round(cv, 4),
        "burstiness_ai_score": round(burstiness_ai_score, 4),
        "type_token_ratio": round(ttr, 4),
        "ttr_ai_score": round(ttr_ai_score, 4),
        "ai_phrase_hits": hit_count,
        "transition_ai_score": round(transition_ai_score, 4),
        "avg_word_length": round(avg_wlen, 4),
        "word_len_ai_score": round(word_len_ai_score, 4),
    }

    return round(min(max(score, 0.0), 1.0), 4), features


# ── Signal 2: LLM Classifier (M4 — fully implemented) ────────────────────────

def llm_signal(text: str) -> tuple[float, list[str]]:
    """
    Calls Groq (llama-3.1-8b-instant) to estimate AI-generation probability.

    The model is prompted to act as a forensic linguist and evaluate:
      - personal voice and lived-experience specificity
      - structural formulaism vs. organic flow
      - generic vs. idiosyncratic phrasing

    Returns (score, key_signals) where score is a float in [0, 1]:
      0.0 → strong evidence of human writing
      1.0 → strong evidence of AI generation

    Falls back to (0.5, [error_message]) if the API call fails, so the
    endpoint never returns a 500 when Groq is unavailable.
    """
    prompt = f"""You are an expert forensic linguist specializing in detecting AI-generated text.

Analyze the text below and estimate how likely it was generated by an AI (vs. written by a human).

CRITICAL: Informal, colloquial, or grammatically loose writing is a STRONG signal of human authorship,
NOT a sign of AI generation. AI models produce polished, structured prose. Humans write messily.

Examples that should score 0.0–0.15 (almost certainly human):
- "guys. GUYS. i just found out my cat has been sneaking into the neighbors house for MONTHS and they've been feeding him a second dinner. no wonder he's been so chunky lol. they even named him?? his name is apparently 'Gerald' over there. his real name is Mochi"
- "does anyone else's brain just refuse to work after 2pm or is that just me. i had one meeting today. ONE. and now im staring at my todo list like its written in ancient greek"
- "hot take: cereal is better as a late night snack than a breakfast. breakfast cereal hits different at 11pm dont @ me. also yes i eat it dry sometimes like a gremlin, milk is optional actually"
- "update on the sourdough starter situation: it's alive!!! day 6 and she's bubbling. named her Brenda. my roommate thinks im losing it but Brenda and i are going to make bread this weekend and he WILL be eating his words (and the bread)"
- "so my landlord said he'd fix the heater 'this week' and it's now been three weeks and 40 degrees in my apartment. i am typing this wearing two hoodies and fingerless gloves like a victorian orphan. sent him another email, we'll see lol"

Human signals (score LOW):
  - Typos, missing punctuation, unconventional capitalization
  - Specific named details only the writer would know (cat named Mochi, roommate named Brenda)
  - Emotional tangents, mid-sentence topic shifts, self-interruption
  - Slang, internet vernacular, abbreviations (lol, dont @, im, idk)
  - Incomplete thoughts, run-on sentences, casual asides in parentheses
  - Informal register: "WAY too much", "honestly?", "like a gremlin"

AI signals (score HIGH):
  - Transition phrases: "furthermore", "it is important to note", "in conclusion", "it is worth noting"
  - Balanced paragraph structure with topic sentence + support + conclusion
  - Generic observations that could apply to any situation
  - No specific named people, places, or concrete sensory details
  - Formal vocabulary used consistently throughout
  - Claims presented without personal stake or lived experience

Score guide:
  0.0–0.2  Almost certainly human  (casual voice, specific details, messy authentic tone)
  0.2–0.4  Probably human          (mostly authentic, minor polish)
  0.4–0.6  Uncertain               (genuinely mixed signals)
  0.6–0.8  Probably AI             (formulaic, generic, structured)
  0.8–1.0  Almost certainly AI     (heavy AI hallmarks, no personal voice at all)

Text to analyze:
\"\"\"
{text[:2500]}
\"\"\"

Reply with ONLY valid JSON — no prose, no markdown fences:
{{"ai_probability": <0.0 to 1.0>, "key_signals": ["<signal1>", "<signal2>", "<signal3>"]}}"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        raw = response.choices[0].message.content.strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            score = float(parsed.get("ai_probability", 0.5))
            score = round(max(0.0, min(1.0, score)), 4)
            return score, parsed.get("key_signals", [])
    except Exception as exc:
        return 0.5, [f"LLM unavailable: {exc}"]

    return 0.5, ["could not parse LLM response"]


# ── Confidence Scorer (M4 — full formula with disagreement penalty) ───────────

def compute_confidence(linguistic_score: float, llm_score: float) -> tuple[float, float]:
    """
    Combines two signal scores into (combined_score, confidence).

    combined_score = 0.40 × linguistic + 0.60 × llm
    confidence     = |combined − 0.5| × 2 × (1 − |linguistic − llm|)

    The first factor measures how far the combined score is from center
    (0.5 = pure uncertainty). The second factor penalizes disagreement:
    if one signal says 0.8 and the other says 0.2, confidence collapses
    toward 0 even though the average is 0.5 — honest uncertainty rather
    than a spurious average.

    A score of 0.51 → confidence ≈ 0.02 (uncertain).
    A score of 0.95 with both signals agreeing → confidence ≈ 0.90.
    """
    combined = round(0.40 * linguistic_score + 0.60 * llm_score, 4)
    disagreement = abs(linguistic_score - llm_score)
    confidence = round(abs(combined - 0.5) * 2.0 * (1.0 - disagreement), 4)
    return combined, confidence


# ── Label Generator (M5 — fully implemented) ─────────────────────────────────

def get_transparency_label(combined_score: float, confidence: float) -> tuple[str, str]:
    """
    Maps (combined_score, confidence) to one of three transparency label variants.

    Thresholds (from planning.md Design Decisions § Transparency Label Text):
      high_confidence_ai    → combined >= 0.70 AND confidence >= 0.65
      high_confidence_human → combined <= 0.30 AND confidence >= 0.65
      uncertain             → everything else

    Both gates must pass simultaneously. A combined score of 0.90 with confidence
    of 0.50 (signals disagreed) still returns uncertain — the system must be
    directionally clear AND internally consistent to commit to a high-confidence label.

    False-positive asymmetry: thresholds are intentionally conservative so the system
    defaults to uncertain rather than risk labeling human work as AI-generated.
    """
    high_confidence = confidence >= 0.65

    if high_confidence and combined_score >= 0.70:
        return (
            "high_confidence_ai",
            (
                "AI-Generated Content: Our analysis strongly indicates this content was "
                "produced by an AI writing system. Multiple signals — including structural "
                "patterns, vocabulary characteristics, and stylistic markers — point to AI "
                "authorship. If you wrote this yourself and believe this assessment is wrong, "
                "you can submit an appeal and a human moderator will review your case."
            ),
        )

    if high_confidence and combined_score <= 0.30:
        return (
            "high_confidence_human",
            (
                "Human-Created Content: Our analysis strongly indicates this content was "
                "written by a human. The writing shows characteristics consistent with "
                "authentic human authorship: varied sentence rhythm, personal voice, and "
                "idiosyncratic word choices. No significant AI-generation signals were detected."
            ),
        )

    return (
        "uncertain",
        (
            "Origin Uncertain: We could not determine with confidence whether this content "
            "was written by a human or generated by AI. It may be entirely human-written, "
            "AI-generated, or a blend of both. Our detection signals gave mixed or weak "
            "results. Readers should draw their own conclusions, and creators who feel this "
            "is inaccurate are welcome to submit an appeal."
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify(
        {
            "service": "Provenance Guard",
            "version": "1.0",
            "endpoints": ["POST /submit", "POST /appeal/<content_id>", "GET /log", "GET /status/<content_id>"],
        }
    )


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour; 200 per day")
def submit():
    data = request.get_json(silent=True)

    if not data or "text" not in data:
        return jsonify({"error": "Request body must be JSON with a 'text' field."}), 400

    if "creator_id" not in data:
        return jsonify({"error": "Request body must include a 'creator_id' field."}), 400

    text = data["text"]
    creator_id = str(data["creator_id"]).strip()

    if not isinstance(text, str) or len(text.strip()) < 50:
        return jsonify({"error": "Text must be a string of at least 50 characters."}), 400

    if len(text) > 10_000:
        return jsonify({"error": "Text exceeds the 10,000-character limit."}), 400

    content_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"

    # Signal 1: fully implemented in M3
    ling_score, ling_features = linguistic_signal(text)

    # Signal 2: stub until M4
    llm_score, llm_signals = llm_signal(text)

    # Confidence: simplified stub until M4
    combined_score, confidence = compute_confidence(ling_score, llm_score)

    # Label: stub until M5
    label_variant, label_text = get_transparency_label(combined_score, confidence)

    attribution = "AI-generated" if combined_score >= 0.50 else "Human-written"

    response_body = {
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        # Top-level shorthand fields (required by M3 spec)
        "attribution": attribution,
        "confidence": confidence,
        "label": label_text,
        # Full detail
        "ai_probability": combined_score,
        "transparency_label": {
            "variant": label_variant,
            "text": label_text,
        },
        "signals": {
            "linguistic": {
                "score": ling_score,
                "weight": 0.40,
                "features": ling_features,
            },
            "llm": {
                "score": llm_score,
                "weight": 0.60,
                "key_signals": llm_signals,
            },
        },
        "status": "classified",
    }

    # Persist classification to audit log
    audit_entry = {
        "event": "classification",
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "text_preview": text[:120].rstrip() + ("…" if len(text) > 120 else ""),
        "attribution": attribution,
        "label_variant": label_variant,
        "signals": {
            "linguistic_score": ling_score,
            "llm_score": llm_score,
            "combined_score": combined_score,
            "confidence": confidence,
        },
        "status": "classified",
    }

    log = load_audit_log()
    log.append(audit_entry)
    save_audit_log(log)

    return jsonify(response_body), 200


@app.route("/appeal/<content_id>", methods=["POST"])
@limiter.limit("5 per hour")
def appeal(content_id):
    data = request.get_json(silent=True)

    if not data or "creator_reasoning" not in data:
        return jsonify({"error": "Request body must include a 'creator_reasoning' field."}), 400

    reasoning = str(data["creator_reasoning"]).strip()
    if len(reasoning) < 20:
        return jsonify({"error": "Please provide a more detailed explanation (at least 20 characters)."}), 400

    log = load_audit_log()

    # Find the original classification entry
    original = None
    original_idx = None
    for i, entry in enumerate(log):
        if entry.get("content_id") == content_id and entry.get("event") == "classification":
            original = entry
            original_idx = i
            break

    if original is None:
        return jsonify({"error": f"No classification found for content_id '{content_id}'."}), 404

    if original.get("status") == "under_review":
        return jsonify({"error": "An appeal is already pending for this content."}), 409

    appeal_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"

    # Update original entry status in-place
    log[original_idx]["status"] = "under_review"
    log[original_idx]["appeal_id"] = appeal_id

    # Append appeal event alongside the original decision
    appeal_entry = {
        "event": "appeal",
        "appeal_id": appeal_id,
        "content_id": content_id,
        "timestamp": timestamp,
        "original_attribution": original.get("attribution"),
        "original_signals": original.get("signals"),
        "original_label_variant": original.get("label_variant"),
        "creator_reasoning": reasoning,
        "status": "pending_review",
    }
    log.append(appeal_entry)
    save_audit_log(log)

    return jsonify({
        "appeal_id": appeal_id,
        "content_id": content_id,
        "status": "appeal_submitted",
        "message": (
            "Your appeal has been received. This content is now marked as 'under review' "
            "and a human moderator will assess your case."
        ),
        "original_classification": {
            "attribution": original.get("attribution"),
            "signals": original.get("signals"),
            "label_variant": original.get("label_variant"),
        },
        "creator_reasoning": reasoning,
        "timestamp": timestamp,
    }), 200


@app.route("/status/<content_id>", methods=["GET"])
def status(content_id):
    log = load_audit_log()
    classification = None
    appeals = []
    for entry in log:
        if entry.get("content_id") == content_id:
            if entry.get("event") == "classification":
                classification = entry
            elif entry.get("event") == "appeal":
                appeals.append(entry)

    if classification is None:
        return jsonify({"error": f"No content found with id '{content_id}'."}), 404

    return jsonify({
        "content_id": content_id,
        "current_status": classification.get("status"),
        "classification": classification,
        "appeals": appeals,
    }), 200


@app.route("/log", methods=["GET"])
def get_log():
    return jsonify({"entries": load_audit_log()}), 200


if __name__ == "__main__":
    app.run(port=5001, debug=True)
