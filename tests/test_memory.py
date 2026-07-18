import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import memory


def fresh_repo():
    r = Path(tempfile.mkdtemp())
    (r / ".agentic").mkdir()
    return r


class TestMemory(unittest.TestCase):
    def test_empty(self):
        r = fresh_repo()
        self.assertEqual(memory.load(r), {})
        self.assertEqual(memory.as_prompt(r), "")

    def test_merge_creates_and_dedups(self):
        r = fresh_repo()
        memory.merge(r, {memory.DECISIONS: ["Auth uses JWT", "No Redis (deployment)"]})
        memory.merge(r, {memory.DECISIONS: ["auth uses jwt"],  # case/space dup -> ignored
                         memory.FAILURES: ["Docker image exceeded size limit"]})
        data = memory.load(r)
        self.assertEqual(data[memory.DECISIONS], ["Auth uses JWT", "No Redis (deployment)"])
        self.assertEqual(data[memory.FAILURES], ["Docker image exceeded size limit"])
        txt = memory.as_prompt(r)
        self.assertIn("Auth uses JWT", txt)
        self.assertIn("Past failures", txt)

    def test_hand_edits_preserved(self):
        r = fresh_repo()
        (r / ".agentic" / "memory.md").write_text(
            "# loopd project memory\n\n## Architecture decisions\n- Use PostgreSQL JSONB\n")
        memory.merge(r, {memory.TODOS: ["Replace polling with websockets"]})
        data = memory.load(r)
        self.assertIn("Use PostgreSQL JSONB", data[memory.DECISIONS])
        self.assertIn("Replace polling with websockets", data[memory.TODOS])

    def test_cap_per_section(self):
        r = fresh_repo()
        memory.merge(r, {memory.FAILURES: [f"failure {i}" for i in range(80)]})
        self.assertEqual(len(memory.load(r)[memory.FAILURES]), memory._MAX_PER_SECTION)
        # newest kept
        self.assertIn("failure 79", memory.load(r)[memory.FAILURES])

    def test_from_directive(self):
        upd = memory.from_directive_memory({"decisions": ["a"], "todos": ["b"]})
        self.assertEqual(upd[memory.DECISIONS], ["a"])
        self.assertEqual(upd[memory.TODOS], ["b"])
        self.assertEqual(upd[memory.FAILURES], [])

    def test_render_roundtrip(self):
        data = {memory.DECISIONS: ["x"], memory.TODOS: ["y"]}
        self.assertIn("## Architecture decisions", memory.render(data))
        self.assertIn("- x", memory.render(data))


if __name__ == "__main__":
    unittest.main()
