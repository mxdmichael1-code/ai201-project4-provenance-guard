# Provenance Guard

Provenance Guard is a Flask backend that classifies submitted creative text, reports confidence honestly, shows a reader-facing transparency label, logs each decision, and lets creators appeal a classification.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Optional Doubao support:

```bash
cp .env.example .env
# add DOUBAO_API_KEY=...
# set DOUBAO_MODEL to your Doubao model or Ark endpoint ID
```

If `DOUBAO_API_KEY` or `DOUBAO_MODEL` is not set, the semantic signal uses a deterministic local proxy so the project remains runnable without secrets.

## API

### Submit Content

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that stakeholders across various sectors must collaborate to ensure responsible deployment.", "creator_id": "test-user-1"}' | python -m json.tool
```

The response includes:

- `content_id`
- `attribution`
- `ai_probability`
- `confidence_score`
- `signal_agreement`
- `signal_std_dev`
- `transparency_label`
- all individual signal scores

### Appeal

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-CONTENT-ID-HERE", "creator_reasoning": "I wrote this myself from personal experience. My style may appear formal because English is not my first language."}' | python -m json.tool
```

### Audit Log

```bash
curl -s http://localhost:5000/log | python -m json.tool
```

## Architecture Overview

A submission enters `POST /submit`, passes Flask validation and rate limiting, then goes through three independent detection signals. The scorer combines those signals into an AI probability and confidence score, the label generator maps the score to reader-facing text, and the SQLite audit log records the decision before JSON is returned.

Appeals enter `POST /appeal`, reference an existing `content_id`, update the content status to `under_review`, and append an appeal event with the creator's reasoning and original scores.

See [planning.md](planning.md) for the architecture diagram.

## Detection Signals

1. Semantic LLM/Doubao assessment: uses a configured Doubao model through Volcengine Ark's OpenAI-compatible API when configured, or a deterministic local proxy without an API key. It captures holistic signs like generic framing, balanced tone, and lack of lived specificity. It can miss heavily edited AI text and can over-score polished human writing.
2. Stylometric uniformity: measures sentence length variance, vocabulary diversity, punctuation density, and average word length. It captures structural regularity. It can misread formal essays, poems, or non-native English writing.
3. Pattern scan: checks repeated openings, formulaic phrases, and connective language. It captures surface patterns common in generic AI output. It is the easiest signal to evade and can penalize academic style.

## Confidence Scoring

Each signal returns an AI probability from `0.0` to `1.0`. The ensemble weighting is:

- Semantic LLM/Doubao: 50%
- Stylometric uniformity: 25%
- Pattern scan: 25%

The combined `ai_probability` maps to labels:

- `ai_probability >= 0.70` and `confidence_score >= 0.70`: `likely_ai`
- `ai_probability <= 0.28` and `confidence_score >= 0.70`: `likely_human`
- otherwise: `uncertain`

The API also returns `confidence_score`, which is certainty in the displayed attribution after accounting for signal agreement. It starts with distance from `0.50`, then lowers confidence when the three weighted signals disagree.

```text
directional_certainty = abs(ai_probability - 0.50) * 2
signal_agreement = 1 - min(signal_std_dev / 0.50, 1)
confidence_score = 0.50 + (0.50 * directional_certainty * signal_agreement)
```

This avoids treating a 0.51 score like a confident result and also avoids over-trusting a result where one signal strongly disagrees with the others. The high-confidence AI and high-confidence human labels are only used when both the direction and confidence thresholds are met.

Example high-confidence AI-like submission:

```json
{
  "text": "Artificial intelligence is a transformative paradigm shift across various sectors...",
  "example_ai_probability": 0.803,
  "example_confidence_score": 0.754,
  "example_signal_agreement": 0.835,
  "example_attribution": "likely_ai"
}
```

Example lower-confidence/borderline submission:

```json
{
  "text": "The relationship between monetary policy and asset price inflation...",
  "example_ai_probability": 0.404,
  "example_confidence_score": 0.555,
  "example_signal_agreement": 0.569,
  "example_attribution": "uncertain"
}
```

## Transparency Labels

| Variant | Exact text |
| --- | --- |
| High-confidence AI | "Provenance Guard: This work shows strong signs of AI generation. Confidence is high, but this is not a final judgment; the creator can appeal or provide more context." |
| High-confidence human | "Provenance Guard: This work shows strong signs of human authorship. Confidence is high based on the signals available." |
| Uncertain | "Provenance Guard: The authorship signals are mixed. We are not labeling this work as AI-generated or human-written with high confidence." |

## Appeals Workflow

Creators submit an appeal with `content_id` and `creator_reasoning`. The system validates the content exists, updates its status to `under_review`, and writes an appeal event to the structured audit log. A reviewer would see the content ID, creator ID, original attribution, signal scores, text excerpt, status, and appeal reasoning.

## Rate Limiting

`POST /submit` is limited to `10 per minute;100 per day` per remote address.

Reasoning: a real writer might submit several drafts in a short editing session, so the minute limit allows normal use. A script trying to flood the classifier will quickly hit `429 Too Many Requests`. The daily limit keeps one client from consuming the service repeatedly while still allowing normal creative-platform activity.

Rate-limit test command:

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a test submission for rate limit testing purposes only, with enough words to pass validation.", "creator_id": "ratelimit-test"}'
done
```

Expected evidence:

```text
200
200
200
200
200
200
200
200
200
200
429
429
```

## Audit Log Sample

The canonical log is stored in SQLite and exposed through `GET /log`. A representative sample with three structured entries:

```json
{
  "entries": [
    {
      "event_type": "appeal",
      "content_id": "6f6d6b3b-6fc6-4d15-96d8-8a5a8a9d86c8",
      "creator_id": "test-user-1",
      "attribution": "uncertain",
      "ai_probability": 0.404,
      "confidence_score": 0.555,
      "signal_agreement": 0.569,
      "signal_std_dev": 0.215,
      "status": "under_review",
      "appeal_reasoning": "I wrote this myself from personal experience."
    },
    {
      "event_type": "classification",
      "content_id": "6f6d6b3b-6fc6-4d15-96d8-8a5a8a9d86c8",
      "creator_id": "test-user-1",
      "attribution": "uncertain",
      "ai_probability": 0.404,
      "confidence_score": 0.555,
      "signal_agreement": 0.569,
      "signal_std_dev": 0.215,
      "status": "classified",
      "signals": [
        {"name": "semantic_llm", "score": 0.39},
        {"name": "stylometric", "score": 0.58},
        {"name": "pattern_scan", "score": 0.32}
      ]
    },
    {
      "event_type": "classification",
      "content_id": "d724f6c1-0f75-49ca-a0a1-64c7afc6a3f2",
      "creator_id": "test-user-2",
      "attribution": "likely_ai",
      "ai_probability": 0.803,
      "confidence_score": 0.754,
      "signal_agreement": 0.835,
      "signal_std_dev": 0.082,
      "status": "classified"
    }
  ]
}
```

## Known Limitations

Formal human writing is the biggest false-positive risk. A careful academic paragraph can have long sentences, low variance, and connective phrases, causing the stylometric and pattern signals to push toward AI even when a human wrote it. Poetry with repeated lines can create the same problem because repetition is meaningful in poetry but suspicious to a simple pattern scan.

If this were deployed for real, the audit log endpoint would require authentication, the rate limiter would use Redis instead of in-memory storage, and the model calibration would be tested against a labeled dataset.

## Spec Reflection

Writing the thresholds in `planning.md` first made the label logic straightforward: the implementation uses the same `0.70` and `0.28` cutoffs rather than inventing a binary 0.5 split during coding.

The implementation diverged from the original recommended two-signal design by adding a third pattern-scan signal. I added it to make the ensemble more transparent and to complete the stretch ensemble feature while keeping each signal easy to inspect in the audit log.

## AI Usage

1. I directed the AI tool to turn the architecture and detection-signal spec into a Flask skeleton with `/submit`, SQLite logging, and signal function boundaries. I revised the output to keep the Doubao dependency optional so the app runs without committing secrets.
2. I directed the AI tool to implement the confidence scoring and label mapping from the planning thresholds. I overrode binary-style scoring and kept `ai_probability`, `confidence_score`, `signal_agreement`, and `signal_std_dev` so uncertain or internally conflicted results are represented honestly.
3. I directed the AI tool to generate appeal handling and audit-log examples. I revised the appeal path so it logs the original signal scores alongside the creator's reasoning.




