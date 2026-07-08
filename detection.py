import json
import os
import math
import re
from statistics import mean, variance

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


AI_PHRASES = [
    "artificial intelligence",
    "it is important to note",
    "in conclusion",
    "furthermore",
    "moreover",
    "delve",
    "landscape",
    "transformative",
    "paradigm shift",
    "stakeholders",
    "responsible deployment",
    "ethical implications",
    "across various sectors",
]


def clamp(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, value))


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def words(text):
    return re.findall(r"[A-Za-z']+", text.lower())


def classify(ai_probability, confidence_score=None):
    # The AI threshold is deliberately above 0.5 because false positives are
    # harmful on a creative platform; borderline work stays "uncertain."
    has_high_confidence = confidence_score is None or confidence_score >= 0.70
    if ai_probability >= 0.70 and has_high_confidence:
        return "likely_ai"
    if ai_probability <= 0.28 and has_high_confidence:
        return "likely_human"
    return "uncertain"


def confidence_from_probability(ai_probability, signal_std_dev):
    # Confidence means certainty in the displayed attribution. It increases as
    # the combined score moves away from 0.5, then drops if signals disagree.
    directional_certainty = abs(ai_probability - 0.5) * 2
    signal_agreement = 1 - clamp(signal_std_dev / 0.5)
    confidence = 0.5 + (0.5 * directional_certainty * signal_agreement)
    return round(clamp(confidence), 3)


def local_semantic_proxy(text):
    lowered = text.lower()
    matched = [phrase for phrase in AI_PHRASES if phrase in lowered]
    word_list = words(text)
    sentence_list = sentences(text)

    formality_hits = sum(1 for word in word_list if len(word) >= 10)
    formality_rate = formality_hits / max(len(word_list), 1)
    personal_markers = len(re.findall(r"\b(i|me|my|we|our|honestly|ok|yeah|lol)\b", lowered))
    personal_rate = personal_markers / max(len(word_list), 1)
    avg_sentence = mean([len(words(sentence)) for sentence in sentence_list]) if sentence_list else 0

    score = 0.42
    score += min(len(matched) * 0.07, 0.28)
    score += clamp((formality_rate - 0.08) * 1.8, 0, 0.18)
    score += clamp((avg_sentence - 18) / 70, 0, 0.12)
    score -= clamp(personal_rate * 5, 0, 0.22)

    return {
        "name": "semantic_llm",
        "score": round(clamp(score), 3),
        "details": {
            "mode": "local_proxy_no_api_key",
            "matched_phrases": matched,
            "personal_marker_rate": round(personal_rate, 3),
            "note": "Set DOUBAO_API_KEY and DOUBAO_MODEL to use Doubao through Volcengine Ark.",
        },
    }


def doubao_semantic_signal(text):
    api_key = os.getenv("DOUBAO_API_KEY")
    model = os.getenv("DOUBAO_MODEL")
    base_url = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")

    if not api_key or not model or OpenAI is None:
        return local_semantic_proxy(text)

    client = OpenAI(api_key=api_key, base_url=base_url)
    prompt = (
        "Classify whether this submitted creative text is AI-generated or "
        "human-written. Return only JSON with keys ai_probability and reasoning. "
        "ai_probability must be a number from 0 to 1. Text:\n\n"
        f"{text[:4000]}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a cautious provenance classifier. Avoid overclaiming.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        score = clamp(float(parsed.get("ai_probability", 0.5)))
        reasoning = str(parsed.get("reasoning", "No reasoning returned."))
        return {
            "name": "semantic_llm",
            "score": round(score, 3),
            "details": {
                "mode": "doubao_ark",
                "base_url": base_url,
                "model": model,
                "reasoning": reasoning,
            },
        }
    except Exception as exc:
        fallback = local_semantic_proxy(text)
        fallback["details"]["doubao_error"] = str(exc)
        return fallback


def stylometric_signal(text):
    word_list = words(text)
    sentence_list = sentences(text)
    sentence_lengths = [len(words(sentence)) for sentence in sentence_list] or [0]
    unique_words = len(set(word_list))
    total_words = max(len(word_list), 1)

    if len(sentence_lengths) > 1:
        sentence_variance = variance(sentence_lengths)
    else:
        sentence_variance = 0.0

    type_token_ratio = unique_words / total_words
    punctuation_density = len(re.findall(r"[,;:]", text)) / max(total_words, 1)
    avg_word_length = mean([len(word) for word in word_list]) if word_list else 0

    uniformity_score = 1 - clamp(sentence_variance / 80)
    vocabulary_score = clamp((0.72 - type_token_ratio) / 0.45)
    punctuation_score = clamp((punctuation_density - 0.03) / 0.12)
    word_length_score = clamp((avg_word_length - 4.7) / 2.2)

    score = (
        uniformity_score * 0.35
        + vocabulary_score * 0.30
        + punctuation_score * 0.15
        + word_length_score * 0.20
    )

    return {
        "name": "stylometric",
        "score": round(clamp(score), 3),
        "details": {
            "word_count": total_words,
            "sentence_count": len(sentence_list),
            "sentence_length_variance": round(sentence_variance, 3),
            "type_token_ratio": round(type_token_ratio, 3),
            "punctuation_density": round(punctuation_density, 3),
            "average_word_length": round(avg_word_length, 3),
        },
    }


def pattern_signal(text):
    lowered = text.lower()
    matched = [phrase for phrase in AI_PHRASES if phrase in lowered]
    sentence_list = sentences(text)
    openings = []
    for sentence in sentence_list:
        sentence_words = words(sentence)
        if sentence_words:
            openings.append(sentence_words[0])

    repeated_openings = 0
    if openings:
        repeated_openings = len(openings) - len(set(openings))
    repeated_opening_rate = repeated_openings / max(len(openings), 1)

    balanced_connectors = len(
        re.findall(r"\b(while|however|therefore|additionally|equally|both|on the other hand)\b", lowered)
    )
    connector_rate = balanced_connectors / max(len(words(text)), 1)

    score = 0.18
    score += min(len(matched) * 0.09, 0.45)
    score += clamp(repeated_opening_rate * 0.35, 0, 0.20)
    score += clamp(connector_rate * 8, 0, 0.17)

    return {
        "name": "pattern_scan",
        "score": round(clamp(score), 3),
        "details": {
            "matched_phrases": matched,
            "repeated_opening_rate": round(repeated_opening_rate, 3),
            "connector_rate": round(connector_rate, 3),
        },
    }


def analyze_text(text):
    signals = [
        doubao_semantic_signal(text),
        stylometric_signal(text),
        pattern_signal(text),
    ]
    weights = {
        "semantic_llm": 0.50,
        "stylometric": 0.25,
        "pattern_scan": 0.25,
    }
    # Weighted averaging keeps the three signals visible in the audit log while
    # still returning one decision score to the API caller.
    ai_probability = sum(signal["score"] * weights[signal["name"]] for signal in signals)
    weighted_variance = sum(
        weights[signal["name"]] * ((signal["score"] - ai_probability) ** 2)
        for signal in signals
    )
    signal_std_dev = math.sqrt(weighted_variance)
    signal_agreement = 1 - clamp(signal_std_dev / 0.5)

    return {
        "ai_probability": round(clamp(ai_probability), 3),
        "confidence_score": confidence_from_probability(ai_probability, signal_std_dev),
        "signal_agreement": round(signal_agreement, 3),
        "signal_std_dev": round(signal_std_dev, 3),
        "signals": signals,
    }
