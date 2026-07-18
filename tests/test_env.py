import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.env import load_dotenv


class TestDotenv(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env_path = self.dir / ".env"
        self._added = []

    def tearDown(self):
        for k in self._added:
            os.environ.pop(k, None)

    def _set(self, body: str):
        self.env_path.write_text(body)

    def _load(self, *keys):
        # Ensure a clean slate for the keys under test, then load.
        for k in keys:
            os.environ.pop(k, None)
            self._added.append(k)
        load_dotenv(self.env_path)

    def test_strips_unquoted_inline_comment(self):
        self._set("BUDGET_USD=25  # dollars per run\n")
        self._load("BUDGET_USD")
        self.assertEqual(os.environ["BUDGET_USD"], "25")

    def test_hash_without_leading_space_is_kept(self):
        # A '#' that is part of the value (no preceding space) is NOT a comment.
        self._set("TOKEN=abc#123\n")
        self._load("TOKEN")
        self.assertEqual(os.environ["TOKEN"], "abc#123")

    def test_quoted_value_keeps_hash(self):
        self._set('MSG="hello # world"  # trailing note\n')
        self._load("MSG")
        self.assertEqual(os.environ["MSG"], "hello # world")

    def test_plain_value_unchanged(self):
        self._set("DEV_MODEL=claude-opus-4-8\n")
        self._load("DEV_MODEL")
        self.assertEqual(os.environ["DEV_MODEL"], "claude-opus-4-8")

    def test_existing_env_not_overwritten(self):
        os.environ["ALREADY_SET"] = "keep"
        self._added.append("ALREADY_SET")
        self.env_path.write_text("ALREADY_SET=changed # note\n")
        load_dotenv(self.env_path)
        self.assertEqual(os.environ["ALREADY_SET"], "keep")


if __name__ == "__main__":
    unittest.main()
