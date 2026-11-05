import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.culture import _connect
from app.intake import (
    create_feedback_submission,
    create_mentorship_request,
    initialize_intake_store,
)


class IntakePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="kokwet-intake-")
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "DATABASE_URL": "",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
                "KOKWET_DB_PATH": str(Path(self.temp_dir.name) / "intake.sqlite3"),
                "PYTHON_DOTENV_DISABLED": "1",
            },
            clear=True,
        )
        self.environment.start()
        initialize_intake_store()

    def tearDown(self):
        self.environment.stop()
        self.temp_dir.cleanup()

    def test_feedback_and_mentorship_requests_persist(self):
        mentorship = create_mentorship_request("0712345678", "Career planning")
        feedback = create_feedback_submission("Please add more youth workshops.")

        with _connect() as connection:
            saved_mentorship = connection.execute(
                "SELECT phone, focus, status FROM mentorship_requests WHERE id = ?",
                (mentorship["id"],),
            ).fetchone()
            saved_feedback = connection.execute(
                "SELECT message FROM feedback_submissions WHERE id = ?",
                (feedback["id"],),
            ).fetchone()

        self.assertEqual(saved_mentorship["phone"], "0712345678")
        self.assertEqual(saved_mentorship["focus"], "Career planning")
        self.assertEqual(saved_mentorship["status"], "Pending assignment")
        self.assertEqual(saved_feedback["message"], "Please add more youth workshops.")


if __name__ == "__main__":
    unittest.main()
