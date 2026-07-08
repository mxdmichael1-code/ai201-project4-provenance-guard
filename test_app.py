import os
import tempfile
import unittest

os.environ["PROVENANCE_DB"] = tempfile.NamedTemporaryFile(delete=False).name

from app import create_app  # noqa: E402


class ProvenanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_submit_returns_classification_and_log_entry(self):
        response = self.client.post(
            "/submit",
            json={
                "creator_id": "test-user",
                "text": (
                    "Artificial intelligence represents a transformative paradigm "
                    "shift in modern society. Furthermore, stakeholders across "
                    "various sectors must collaborate on responsible deployment."
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("content_id", payload)
        self.assertIn(payload["attribution"], {"likely_ai", "likely_human", "uncertain"})
        self.assertIn("signal_agreement", payload)
        self.assertIn("signal_std_dev", payload)
        self.assertEqual(len(payload["signals"]), 3)

        log_response = self.client.get("/log")
        self.assertEqual(log_response.status_code, 200)
        entries = log_response.get_json()["entries"]
        self.assertGreaterEqual(len(entries), 1)
        self.assertEqual(entries[0]["event_type"], "classification")
        self.assertIn("signal_agreement", entries[0])
        self.assertIn("signal_std_dev", entries[0])

    def test_appeal_updates_status(self):
        submit_response = self.client.post(
            "/submit",
            json={
                "creator_id": "test-user",
                "text": (
                    "ok so i finally tried that new ramen place downtown and "
                    "honestly the broth was fine but way too salty. I was "
                    "thirsty for hours and probably will not go back soon."
                ),
            },
        )
        content_id = submit_response.get_json()["content_id"]

        appeal_response = self.client.post(
            "/appeal",
            json={
                "content_id": content_id,
                "creator_reasoning": "I wrote this from my own experience.",
            },
        )
        self.assertEqual(appeal_response.status_code, 200)
        self.assertEqual(appeal_response.get_json()["status"], "under_review")

        entries = self.client.get("/log").get_json()["entries"]
        self.assertEqual(entries[0]["event_type"], "appeal")
        self.assertEqual(entries[0]["status"], "under_review")
        self.assertIn("own experience", entries[0]["appeal_reasoning"])


if __name__ == "__main__":
    unittest.main()
