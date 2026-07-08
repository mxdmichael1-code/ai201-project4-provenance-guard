# Provenance Guard Planning

## Architecture

```text
Submission flow
Client
  | POST /submit {text, creator_id}
  v
Flask API + rate limiter
  | validated raw text
  v
Detection pipeline
  |--> Signal 1: semantic LLM/Doubao assessment -> ai_probability
  |--> Signal 2: stylometric uniformity metrics -> ai_probability
  |--> Signal 3: AI-pattern phrase/repetition scan -> ai_probability
  v
Confidence scorer
  | weighted score + certainty
  v
Transparency label generator
  | attribution + exact reader-facing label text
  v
SQLite audit log
  | stores decision, signal scores, status
  v
Response JSON {content_id, attribution, confidence_score, signal_agreement, label}

Appeal flow
Client
  | POST /appeal {content_id, creator_reasoning}
  v
Flask API
  | finds original content record
  v
Status update
  | status: under_review
  v
SQLite audit log
  | stores appeal with original decision and creator reasoning
  v
Response JSON {content_id, status, message}
```

A submitted text is validated by the API, passed through independent detection signals, converted into a combined AI probability and confidence score, labeled for readers, and written to the audit log before the response is returned. An appeal references the original `content_id`, records the creator's explanation, changes the content status to `under_review`, and appends an appeal event to the same audit trail.

## Detection Signals

### Signal 1: Semantic LLM/Doubao Assessment

This signal asks a configured Doubao model through Volcengine Ark's OpenAI-compatible chat-completions API to judge whether a text reads as AI-generated or human-written and to return a JSON probability from 0 to 1. It captures holistic semantic and stylistic coherence: generic transitions, over-balanced paragraph structure, unnaturally even tone, and lack of lived specificity. Its output is:

```json
{"name": "semantic_llm", "score": 0.0, "details": {"reasoning": "..."}}
```

Blind spot: an LLM can over-trust polished human writing and may miss heavily edited AI writing. If no API key is configured, the implementation uses a deterministic local proxy so the project remains testable; that fallback is less capable and is documented as a development mode.

### Signal 2: Stylometric Uniformity

This signal computes structural measurements: sentence length variance, vocabulary diversity, punctuation density, and average word length. AI writing often has lower sentence-length variance and a more evenly polished texture, while casual human writing often has more abrupt variation. Its output is:

```json
{"name": "stylometric", "score": 0.0, "details": {"sentence_length_variance": 0.0, "type_token_ratio": 0.0}}
```

Blind spot: formal human writing, poetry, and non-native English writing can be structurally regular and therefore may receive a higher AI probability than they deserve.

### Signal 3: Pattern and Repetition Scan

This stretch ensemble signal checks for overused AI-style phrases, balanced connective language, and repeated sentence openings. It captures surface-level formulaic patterns that a pure stylometric calculation may not see. Its output is:

```json
{"name": "pattern_scan", "score": 0.0, "details": {"matched_phrases": ["..."], "repeated_opening_rate": 0.0}}
```

Blind spot: this signal is easy to evade and can be fooled by human writers using formal academic language.

## Uncertainty Representation

Each signal returns an `ai_probability` from 0 to 1, where 0 means strongly human-like and 1 means strongly AI-like. The combined score is a weighted average:

- Semantic LLM/Doubao: 50%
- Stylometric uniformity: 25%
- Pattern scan: 25%

The API returns two related fields:

- `ai_probability`: the combined probability that the content is AI-generated.
- `confidence_score`: certainty in the displayed attribution, computed from distance away from 0.50 and reduced when the individual weighted signals disagree.
- `signal_std_dev`: weighted standard deviation of the three signal scores.
- `signal_agreement`: a 0 to 1 agreement score derived from `signal_std_dev`, where higher means the three signals are more aligned.

Thresholds:

- `ai_probability >= 0.70` and `confidence_score >= 0.70`: `likely_ai`
- `ai_probability <= 0.28` and `confidence_score >= 0.70`: `likely_human`
- otherwise: `uncertain`

A score near 0.50 means the system should not make a strong claim. A score of 0.60 is only weak evidence and must receive the uncertain label. A score of 0.95 is high-confidence evidence for AI-generated writing only if the supporting signals are reasonably aligned, while a score of 0.05 is high-confidence evidence for human-written writing when the signals also agree. Because the transparency labels use the phrase "Confidence is high," the system only uses the `likely_ai` and `likely_human` labels when the confidence score is at least 0.70. Directional but conflicted cases remain `uncertain`.

The confidence formula is:

```text
directional_certainty = abs(ai_probability - 0.50) * 2
signal_agreement = 1 - min(signal_std_dev / 0.50, 1)
confidence_score = 0.50 + (0.50 * directional_certainty * signal_agreement)
```

`directional_certainty` measures how far the combined score is from the uncertain midpoint. If `ai_probability` is 0.50, directional certainty is 0 because the system is split. If `ai_probability` is 0.95 or 0.05, directional certainty is high because the result is far from the midpoint in either direction. `signal_agreement` measures whether the three signals are aligned. It uses weighted standard deviation, so a low spread between Doubao, stylometric, and pattern scores creates high agreement, while a large spread lowers agreement. The final `confidence_score` combines both ideas: a result is most confident when it is far from 0.50 and the signals agree.

Example: if `ai_probability` is 0.719 but one signal is much lower than the others, the confidence score may only be moderate. In that case the label remains `uncertain`, even though the final score leans AI, because the underlying evidence is partially conflicted.

### Strengths and Weaknesses of This Confidence Approach

The strength of this approach is that it separates direction from certainty. `ai_probability` says whether the text leans AI-generated or human-written, while `confidence_score` says how reliable that displayed attribution is. This is more honest than treating every score above a threshold as equally certain. The formula also rewards signal agreement: when Doubao, stylometric analysis, and pattern scanning all point in the same direction, confidence increases; when they conflict, confidence drops. This makes the system easier to explain to users and graders because the audit log can show not only the final score, but also whether the underlying evidence was aligned.

Another strength is that the approach reflects uncertainty better than a simple binary cutoff. For example, a score slightly above 0.70 will not receive a `likely_ai` label unless the confidence score is also at least 0.70. If the three signals disagree, the confidence score stays moderate and the result remains `uncertain` instead of pretending the system is highly certain. That matters for a creative platform because a false positive can unfairly harm a human creator. By lowering confidence when the signals are conflicted, the system gives reviewers and creators more context.

The weakness is that this is still a heuristic, not a statistically calibrated confidence interval. The confidence score is useful for communication, but it does not prove the system is correct a specific percentage of the time. Standard deviation measures how spread out the signal scores are, but it does not know whether the signals themselves are high quality. If all three weak signals agree for the wrong reason, confidence can still be too high. The formula also depends on design choices such as the signal weights and the `0.50` normalization constant, which are reasonable for this project but would need validation against labeled examples before production use.

This approach may also understate confidence in cases where one signal catches something important that the others miss. For example, Doubao might notice semantic patterns that stylometric metrics cannot detect, or a poem might trigger the pattern scanner because repetition is part of the form. In those cases, disagreement lowers confidence even if one signal is actually more informative. That tradeoff is acceptable here because the goal is not perfect AI detection; the goal is to communicate uncertainty clearly and give creators an appeal path.

The false-positive risk matters more than a false negative on a creative platform. Because of that, the AI threshold is intentionally higher than a simple 0.50 cutoff, and uncertain cases invite context instead of implying wrongdoing.

## Transparency Label Design

| Variant | Exact label text |
| --- | --- |
| High-confidence AI | "Provenance Guard: This work shows strong signs of AI generation. Confidence is high, but this is not a final judgment; the creator can appeal or provide more context." |
| High-confidence human | "Provenance Guard: This work shows strong signs of human authorship. Confidence is high based on the signals available." |
| Uncertain | "Provenance Guard: The authorship signals are mixed. We are not labeling this work as AI-generated or human-written with high confidence." |

The label is shown in the `/submit` response as `transparency_label`, together with the attribution result and scores.

## Appeals Workflow

Any creator who has a `content_id` can submit an appeal with:

- `content_id`
- `creator_reasoning`

When an appeal is received, the API validates that the content exists, updates the stored content status from `classified` to `under_review`, and appends an audit event containing the original classification, original signal scores, and the creator's reasoning. A human reviewer opening the appeal queue would see the content ID, creator ID, original text excerpt, original attribution, scores, timestamp, current status, and creator explanation. Automated reclassification is intentionally out of scope.

## API Surface

- `POST /submit`: accepts `{"text": "...", "creator_id": "..."}` and returns the classification, scores, label, and `content_id`.
- `POST /appeal`: accepts `{"content_id": "...", "creator_reasoning": "..."}` and returns an under-review confirmation.
- `GET /log`: returns recent structured audit log entries.
- `GET /health`: simple operational check.

## Anticipated Edge Cases

- A formal essay by a human, especially academic or policy writing, may have low sentence variation and many connective phrases, pushing the stylometric and pattern signals toward AI.
- A poem with repeated lines and simple vocabulary may look formulaic to the repetition scan even though repetition is an intentional poetic device.
- A non-native English speaker may use careful, regular sentence structures that appear more uniform than casual native-speaker prose.
- A heavily edited AI draft with personal anecdotes added may fall into the uncertain range because semantic and structural signals disagree.

## AI Tool Plan

### M3: Submission Endpoint and First Signal

I will provide the Detection Signals section and Architecture diagram to the AI tool. I will ask for a Flask app skeleton, a `POST /submit` route, SQLite initialization, and the first semantic signal function. I will verify by calling the signal directly with test inputs, then by submitting JSON to `/submit` and checking that `content_id`, attribution, confidence placeholder, and audit entries are returned.

### M4: Second Signal and Confidence Scoring

I will provide Detection Signals, Uncertainty Representation, and Architecture. I will ask for stylometric metrics and a weighted scoring function matching the thresholds above. I will check that clearly AI-like, casual human, formal human, and borderline edited AI examples produce meaningfully different `ai_probability` values and that the audit log stores individual signal scores.

### M5: Production Layer

I will provide Transparency Label Design, Appeals Workflow, and Architecture. I will ask for exact label generation, `POST /appeal`, status updates, rate limiting, and complete log output. I will verify all three label variants are reachable, an appeal changes status to `under_review`, `/log` shows at least three structured entries, and a burst of submissions returns HTTP 429 after the documented limit.
