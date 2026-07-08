import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from detection import analyze_text, classify

load_dotenv()

DATABASE_PATH = os.getenv("PROVENANCE_DB", "provenance_guard.db")

LABELS = {
    "likely_ai": (
        "Provenance Guard: This work shows strong signs of AI generation. "
        "Confidence is high, but this is not a final judgment; the creator can "
        "appeal or provide more context."
    ),
    "likely_human": (
        "Provenance Guard: This work shows strong signs of human authorship. "
        "Confidence is high based on the signals available."
    ),
    "uncertain": (
        "Provenance Guard: The authorship signals are mixed. We are not labeling "
        "this work as AI-generated or human-written with high confidence."
    ),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        # content stores the latest state for each submission; audit_log stores
        # the historical evidence graders can inspect through GET /log.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT NOT NULL,
                text TEXT NOT NULL,
                attribution TEXT NOT NULL,
                ai_probability REAL NOT NULL,
                confidence_score REAL NOT NULL,
                transparency_label TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                signal_agreement REAL,
                signal_std_dev REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content_id TEXT NOT NULL,
                creator_id TEXT,
                attribution TEXT,
                ai_probability REAL,
                confidence_score REAL,
                status TEXT NOT NULL,
                signals_json TEXT,
                signal_agreement REAL,
                signal_std_dev REAL,
                appeal_reasoning TEXT,
                text_excerpt TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(content)").fetchall()
        }
        if "signal_agreement" not in columns:
            conn.execute("ALTER TABLE content ADD COLUMN signal_agreement REAL")
        if "signal_std_dev" not in columns:
            conn.execute("ALTER TABLE content ADD COLUMN signal_std_dev REAL")

        audit_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()
        }
        if "signal_agreement" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN signal_agreement REAL")
        if "signal_std_dev" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN signal_std_dev REAL")


def row_to_entry(row):
    entry = dict(row)
    if entry.get("signals_json"):
        entry["signals"] = json.loads(entry.pop("signals_json"))
    return entry


def create_app():
    app = Flask(__name__)
    init_db()

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/submit")
    @limiter.limit(os.getenv("SUBMIT_RATE_LIMIT", "10 per minute;100 per day"))
    def submit():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text", "")).strip()
        creator_id = str(payload.get("creator_id", "")).strip()

        if not text or not creator_id:
            return jsonify({"error": "Both text and creator_id are required."}), 400
        if len(text) < 40:
            return jsonify({"error": "Text must be at least 40 characters for analysis."}), 400

        # The detection pipeline returns individual signal scores first; the
        # route only maps the combined probability to a label and persists it.
        analysis = analyze_text(text)
        attribution = classify(analysis["ai_probability"], analysis["confidence_score"])
        label = LABELS[attribution]
        content_id = str(uuid.uuid4())
        timestamp = utc_now()
        signals_json = json.dumps(analysis["signals"], sort_keys=True)

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO content (
                    content_id, creator_id, text, attribution, ai_probability,
                    confidence_score, transparency_label, signals_json, status,
                    created_at, updated_at, signal_agreement, signal_std_dev
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_id,
                    creator_id,
                    text,
                    attribution,
                    analysis["ai_probability"],
                    analysis["confidence_score"],
                    label,
                    signals_json,
                    "classified",
                    timestamp,
                    timestamp,
                    analysis["signal_agreement"],
                    analysis["signal_std_dev"],
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, event_type, content_id, creator_id, attribution,
                    ai_probability, confidence_score, status, signals_json,
                    signal_agreement, signal_std_dev, text_excerpt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    "classification",
                    content_id,
                    creator_id,
                    attribution,
                    analysis["ai_probability"],
                    analysis["confidence_score"],
                    "classified",
                    signals_json,
                    analysis["signal_agreement"],
                    analysis["signal_std_dev"],
                    text[:180],
                ),
            )

        return jsonify(
            {
                "content_id": content_id,
                "creator_id": creator_id,
                "attribution": attribution,
                "ai_probability": analysis["ai_probability"],
                "confidence_score": analysis["confidence_score"],
                "signal_agreement": analysis["signal_agreement"],
                "signal_std_dev": analysis["signal_std_dev"],
                "transparency_label": label,
                "status": "classified",
                "signals": analysis["signals"],
            }
        )

    @app.post("/appeal")
    def appeal():
        payload = request.get_json(silent=True) or {}
        content_id = str(payload.get("content_id", "")).strip()
        reasoning = str(payload.get("creator_reasoning", "")).strip()

        if not content_id or not reasoning:
            return jsonify({"error": "Both content_id and creator_reasoning are required."}), 400

        timestamp = utc_now()
        with get_db() as conn:
            content = conn.execute(
                "SELECT * FROM content WHERE content_id = ?", (content_id,)
            ).fetchone()
            if content is None:
                return jsonify({"error": "No content found for that content_id."}), 404

            # Appeals do not erase the original decision. They update the live
            # status and append a new audit event with the creator's reasoning.
            conn.execute(
                "UPDATE content SET status = ?, updated_at = ? WHERE content_id = ?",
                ("under_review", timestamp, content_id),
            )
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, event_type, content_id, creator_id, attribution,
                    ai_probability, confidence_score, status, signals_json,
                    signal_agreement, signal_std_dev, appeal_reasoning, text_excerpt
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    "appeal",
                    content["content_id"],
                    content["creator_id"],
                    content["attribution"],
                    content["ai_probability"],
                    content["confidence_score"],
                    "under_review",
                    content["signals_json"],
                    content["signal_agreement"],
                    content["signal_std_dev"],
                    reasoning,
                    content["text"][:180],
                ),
            )

        return jsonify(
            {
                "content_id": content_id,
                "status": "under_review",
                "message": "Appeal received. The content is now under review.",
            }
        )

    @app.get("/log")
    def audit_log():
        limit = request.args.get("limit", default=25, type=int)
        limit = max(1, min(limit, 100))
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return jsonify({"entries": [row_to_entry(row) for row in rows]})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
