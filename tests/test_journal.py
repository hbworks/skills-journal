"""Dependency-free tests for skills-journal.

    python3 -m unittest discover -s tests -v

Any Python 3.10+ works; use the interpreter Hermes ships (`~/.hermes/tools/python-*/bin/python3`)
when the system one is older.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import journal  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
    )


class JournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "skills"
        self.root.mkdir()

        # Authored skill (never bundled).
        _write(self.root / "authoring" / "mine" / "SKILL.md", "# mine\n")
        _write(self.root / "authoring" / "mine" / "scripts" / "run.py", "print(1)\n")
        # Bundled and pristine.
        _write(self.root / "research" / "arxiv" / "SKILL.md", "# arxiv\n")
        # Bundled but edited by the user.
        _write(self.root / "research" / "edited" / "SKILL.md", "# edited locally\n")

        arxiv_hash = journal.dir_hash(self.root / "research" / "arxiv")
        _write(
            self.root / ".bundled_manifest",
            f"arxiv:{arxiv_hash}\nedited:{'0' * 32}\nmine:{'f' * 32}\n",
        )
        _git(self.root, "init", "-q", "-b", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pristine_detection(self) -> None:
        pristine = journal.pristine_bundled_dirs(self.root)
        self.assertEqual(pristine, ["research/arxiv"])

    def test_gitignore_ignores_only_pristine_bundled(self) -> None:
        journal.ensure_gitignore(self.root)
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/research/arxiv/", text)
        self.assertNotIn("/research/edited/", text)
        self.assertIn(".locks/", text)

    def test_initial_commit_tracks_authored_and_edited_only(self) -> None:
        journal.ensure_gitignore(self.root)
        sha = journal.commit(self.root, extra_note="setup")
        self.assertIsNotNone(sha)

        tracked = _git(self.root, "ls-files").stdout.splitlines()
        self.assertIn("authoring/mine/SKILL.md", tracked)
        self.assertIn("authoring/mine/scripts/run.py", tracked)
        self.assertIn("research/edited/SKILL.md", tracked)
        self.assertNotIn("research/arxiv/SKILL.md", tracked)
        self.assertNotIn("__pycache__", " ".join(tracked))

    def test_commit_is_idempotent_and_reports_nothing_when_clean(self) -> None:
        journal.ensure_gitignore(self.root)
        self.assertIsNotNone(journal.commit(self.root))
        self.assertIsNone(journal.commit(self.root))

    def test_edit_produces_second_commit_with_skill_name(self) -> None:
        journal.ensure_gitignore(self.root)
        journal.commit(self.root)

        _write(self.root / "authoring" / "mine" / "scripts" / "run.py", "print(2)\n")
        self.assertEqual(journal.pending_changes(self.root), ["authoring/mine/scripts/run.py"])
        sha = journal.commit(self.root, tools=["write_file"], session_id="abcdef1234", turn_id="turn-1")
        self.assertIsNotNone(sha)

        subject = _git(self.root, "log", "-1", "--pretty=%s").stdout.strip()
        self.assertEqual(subject, "skills: authoring/mine")
        log = _git(self.root, "log", "-2", "--pretty=%s").stdout.splitlines()
        self.assertEqual(len(log), 2)

    def test_revert_restores_previous_content(self) -> None:
        journal.ensure_gitignore(self.root)
        journal.commit(self.root)
        target = self.root / "authoring" / "mine" / "scripts" / "run.py"
        _write(target, "print(2)\n")
        journal.commit(self.root, tools=["patch"])

        code = _git(self.root, "checkout", "HEAD~1", "--", "authoring/mine/scripts/run.py").returncode
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "print(1)\n")

    def test_skill_name_for_category_only_change(self) -> None:
        self.assertEqual(journal.skill_name_for("research/arxiv/SKILL.md", self.root), "research/arxiv")
        self.assertEqual(journal.skill_name_for("research/DESCRIPTION.md", self.root), "research")

    def test_noop_outside_a_repository(self) -> None:
        other = Path(self._tmp.name) / "plain"
        other.mkdir()
        self.assertIsNone(journal.commit(other))
        self.assertEqual(journal.pending_changes(other), [])


class DetectionTest(unittest.TestCase):
    """post_tool_call filtering (no git needed)."""

    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "skills_journal_init", Path(__file__).resolve().parents[1] / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["skills_journal_init"] = module
        # ``from . import journal`` fails without a package; fall back to the plain import.
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        self.mod = module
        self.root = Path("/tmp/profile/skills")

    def test_skill_manage_always_touches(self) -> None:
        self.assertTrue(self.mod.touches_skills("skill_manage", {"name": "x"}, self.root))

    def test_write_file_inside_and_outside(self) -> None:
        self.assertTrue(
            self.mod.touches_skills(
                "write_file", {"path": "/tmp/profile/skills/a/SKILL.md"}, self.root
            )
        )
        self.assertFalse(
            self.mod.touches_skills("write_file", {"path": "/tmp/elsewhere/a/SKILL.md"}, self.root)
        )

    def test_git_internals_are_not_tracked(self) -> None:
        self.assertFalse(
            self.mod.touches_skills(
                "write_file", {"path": "/tmp/profile/skills/.git/config"}, self.root
            )
        )

    def test_terminal_needs_both_hints(self) -> None:
        self.assertTrue(
            self.mod.touches_skills("terminal", {"command": "echo hi > /tmp/profile/skills/x"}, self.root)
        )
        self.assertFalse(self.mod.touches_skills("terminal", {"command": "ls"}, self.root))


if __name__ == "__main__":
    unittest.main()
