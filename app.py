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

load_dotenv()

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


# ── Signal 2: LLM Classifier — STUB, implemented in M4 ───────────────────────

def llm_signal(text: str) -> tuple[float, list[str]]:
    # Returns maximum uncertainty until M4 wires in the Groq API call.
    return 0.5, ["[M4 stub] LLM signal not yet implemented"]


# ── Confidence Scorer — STUB, full formula implemented in M4 ─────────────────

def compute_confidence(linguistic_score: float, llm_score: float) -> tuple[float, float]:
    # Simplified: no disagreement penalty yet. M4 adds (1 − |ling − llm|) factor.
    combined = round(0.40 * linguistic_score + 0.60 * llm_score, 4)
    confidence = round(abs(combined - 0.5) * 2.0, 4)
    return combined, confidence


# ── Label Generator — STUB, verbatim text and thresholds implemented in M5 ───

def get_transparency_label(combined_score: float, confidence: float) -> tuple[str, str]:
    # Placeholder — M5 replaces this with the three documented label variants.
    return "uncertain", "[M5 stub] Label generation not yet implemented."


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify(
        {
            "service": "Provenance Guard",
            "version": "1.0-m3",
            "implemented": ["POST /submit (Signal 1 live, Signal 2 stub)"],
            "pending": ["M4: llm_signal + confidence formula", "M5: labels + appeals"],
        }
    )


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour; 200 per day")
def submit():
    data = request.get_json(silent=True)

    if not data or "content" not in data:
        return jsonify({"error": "Request body must be JSON with a 'content' field."}), 400

    text = data["content"]

    if not isinstance(text, str) or len(text.strip()) < 50:
        return jsonify({"error": "Content must be a string of at least 50 characters."}), 400

    if len(text) > 10_000:
        return jsonify({"error": "Content exceeds the 10,000-character limit."}), 400

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
        "timestamp": timestamp,
        "attribution": attribution,
        "ai_probability": combined_score,
        "confidence_score": confidence,
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
        "timestamp": timestamp,
        "content_preview": text[:120].rstrip() + ("…" if len(text) > 120 else ""),
        "attribution": attribution,
        "ai_probability": combined_score,
        "confidence_score": confidence,
        "label_variant": label_variant,
        "signals": {
            "linguistic_score": ling_score,
            "llm_score": llm_score,
        },
        "status": "classified",
    }

    log = load_audit_log()
    log.append(audit_entry)
    save_audit_log(log)

    return jsonify(response_body), 200


@app.route("/appeal/<content_id>", methods=["POST"])
def appeal(content_id):
    # Implemented in M5
    return jsonify({"error": "Not implemented yet — coming in M5."}), 501


@app.route("/status/<content_id>", methods=["GET"])
def status(content_id):
    # Implemented in M5
    return jsonify({"error": "Not implemented yet — coming in M5."}), 501


@app.route("/log", methods=["GET"])
def get_log():
    log = load_audit_log()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    event_filter = request.args.get("event")

    filtered = [e for e in log if not event_filter or e.get("event") == event_filter]
    return jsonify(
        {
            "total_entries": len(filtered),
            "offset": offset,
            "limit": limit,
            "entries": filtered[offset : offset + limit],
        }
    ), 200


if __name__ == "__main__":
    app.run(port=5000, debug=True)
