"""Tests for tools/mine.py.

Run with: python3 -m unittest discover tests
"""

import sqlite3
import tempfile
import unittest
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "tools" / "mine.py"


def _load_mine_module():
    spec = importlib.util.spec_from_file_location("mine", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mine = _load_mine_module()


# A trimmed schema with the same shape as the real engram store (see
# PRAGMA table_info(observations) against ~/.engram/engram.db). Only the
# columns mine.py actually reads are populated by the fixtures below; the
# rest exist so verify_schema()'s column check has something real to pass.
SCHEMA = """
CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    project TEXT,
    scope TEXT NOT NULL DEFAULT 'project',
    topic_key TEXT,
    normalized_hash TEXT,
    revision_count INTEGER NOT NULL DEFAULT 1,
    duplicate_count INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT,
    sync_id TEXT,
    review_after TEXT,
    expires_at TEXT,
    embedding BLOB,
    embedding_model TEXT,
    embedding_created_at TEXT,
    pinned BOOLEAN NOT NULL DEFAULT 0
);
"""


def make_fixture_db(path, rows):
    """rows: iterable of (id, type, title, content, project, created_at, deleted_at)."""
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO observations "
        "(id, type, title, content, project, created_at, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def make_lessons(rows):
    """rows: iterable of (obs_id, project, created_at, title, learned_text)."""
    lessons = []
    for obs_id, project, created_at, title, text in rows:
        tokens = frozenset(mine.normalize(text))
        lessons.append(mine.Lesson(obs_id, project, created_at, title, text, tokens))
    return lessons


class TestExtractLearned(unittest.TestCase):
    def test_extracts_up_to_next_marker(self):
        content = (
            "**What**: did a thing\n"
            "**Learned**: the ci gate never ran on this repo\n"
            "**Next**: check other repos\n"
        )
        self.assertEqual(
            mine.extract_learned(content), "the ci gate never ran on this repo"
        )

    def test_extracts_to_end_of_string_when_no_next_marker(self):
        content = "**What**: did a thing\n**Learned**: last section, nothing after it"
        self.assertEqual(
            mine.extract_learned(content), "last section, nothing after it"
        )

    def test_bare_marker_without_colon_is_handled(self):
        content = "**Learned**\nno colon here, just a newline\n**Fix**: something"
        self.assertEqual(
            mine.extract_learned(content), "no colon here, just a newline"
        )

    def test_missing_marker_returns_none(self):
        content = "**What**: did a thing\n**Why**: because\n"
        self.assertIsNone(mine.extract_learned(content))


class TestNormalize(unittest.TestCase):
    def test_strips_fenced_and_inline_code(self):
        text = (
            "Fixed it by running `pytest -k foo` and this:\n"
            "```\nsome irrelevant code body here\n```\ndone"
        )
        tokens = mine.normalize(text)
        self.assertNotIn("pytest", tokens)
        self.assertNotIn("irrelevant", tokens)  # only reachable from inside the fence

    def test_strips_file_paths_and_long_identifiers(self):
        text = (
            "the bug was in notebooks/tld100_piml_autoencoder_colab.ipynb "
            "and also in cloudstore.go near sessionIdentifier"
        )
        tokens = mine.normalize(text)
        joined = " ".join(tokens)
        self.assertNotIn("notebooks", joined)
        self.assertNotIn("ipynb", joined)
        self.assertNotIn("cloudstore", joined)
        self.assertNotIn("sessionidentifier", joined)

    def test_drops_stopwords_and_punctuation(self):
        tokens = mine.normalize("the gate was, and is, not enough for it.")
        for stopword in ("the", "was", "and", "is", "not", "for", "it"):
            self.assertNotIn(stopword, tokens)
        self.assertNotIn(",", "".join(tokens))


class TestGrouping(unittest.TestCase):
    def test_similar_lessons_in_different_projects_group_together(self):
        lessons = make_lessons(
            [
                (1, "repo-a", "2026-07-21", "t1", "the ci gate never actually ran the test suite here"),
                (2, "repo-b", "2026-08-28", "t2", "the ci gate never actually ran any tests in this pipeline"),
            ]
        )
        member_map = mine.build_groups(lessons, 0.25)
        sizes = sorted(len(idxs) for idxs in member_map.values())
        self.assertIn(2, sizes)

    def test_unrelated_lessons_do_not_group(self):
        lessons = make_lessons(
            [
                (1, "repo-a", "2026-07-21", "t1", "the ci gate never actually ran the test suite here"),
                (2, "repo-b", "2026-08-28", "t2", "unrelated topic about database migrations and schema versions"),
            ]
        )
        member_map = mine.build_groups(lessons, 0.25)
        sizes = sorted(len(idxs) for idxs in member_map.values())
        self.assertEqual(sizes, [1, 1])


class TestRanking(unittest.TestCase):
    ROWS = [
        # a small, two-project group (2 members)
        (1, "repo-a", "2026-07-21", "t1", "credentials leaked through an unset auth env variable"),
        (2, "repo-b", "2026-08-28", "t2", "credentials leaked through an unset auth env variable path"),
        # a larger, single-project group (3 members)
        (3, "repo-c", "2026-03-01", "t3", "refactored the widget rendering pipeline for performance"),
        (4, "repo-c", "2026-03-05", "t4", "refactored the widget rendering pipeline again for performance"),
        (5, "repo-c", "2026-03-09", "t5", "refactored the widget rendering pipeline once more for performance"),
    ]

    def test_two_project_group_ranks_above_larger_one_project_group(self):
        lessons = make_lessons(self.ROWS)
        groups = mine.assemble_groups(lessons, 0.25, 1)
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(len(groups[0]["projects"]), 2)
        self.assertEqual(len(groups[0]["members"]), 2)
        self.assertEqual(len(groups[1]["projects"]), 1)
        self.assertEqual(len(groups[1]["members"]), 3)

    def test_min_projects_filters_out_single_project_groups(self):
        lessons = make_lessons(self.ROWS)
        groups = mine.assemble_groups(lessons, 0.25, 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["projects"]), 2)


class TestDraft(unittest.TestCase):
    def test_draft_contains_verbatim_evidence(self):
        rows = [
            (1, "repo-a", "2026-07-21", "t1", "credentials leaked through an unset auth env variable"),
            (2, "repo-b", "2026-08-28", "t2", "credentials leaked through an unset auth env variable path"),
        ]
        lessons = make_lessons(rows)
        groups = mine.assemble_groups(lessons, 0.25, 2)
        self.assertEqual(len(groups), 1)

        draft = mine.render_draft(groups[0])

        self.assertIn("# TODO: name the class", draft)
        self.assertIn("**Trigger paths:**", draft)
        self.assertIn("credentials leaked through an unset auth env variable", draft)
        self.assertIn("credentials leaked through an unset auth env variable path", draft)
        self.assertIn("observation #1", draft)
        self.assertIn("observation #2", draft)
        self.assertIn("repo-a", draft)
        self.assertIn("repo-b", draft)
        self.assertIn("2026-07-21", draft)
        self.assertIn("2026-08-28", draft)

    def test_unknown_group_id_returns_none(self):
        lessons = make_lessons(
            [(1, "repo-a", "2026-07-21", "t1", "one lonely lesson about nothing shared")]
        )
        groups = mine.assemble_groups(lessons, 0.25, 2)
        self.assertIsNone(mine.find_group(groups, 999))


class TestReadLessonsFromFixtureDb(unittest.TestCase):
    def test_type_and_deleted_and_marker_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fixture.db"
            make_fixture_db(
                db_path,
                [
                    (
                        1,
                        "bugfix",
                        "t1",
                        "**What**: x\n**Why**: y\n**Where**: z\n"
                        "**Learned**: something learned here\n",
                        "repo-a",
                        "2026-07-21 10:00:00",
                        None,
                    ),
                    (
                        2,
                        "session_summary",
                        "t2",
                        "**Learned**: excluded by type, not by content\n",
                        "repo-a",
                        "2026-07-21 10:00:00",
                        None,
                    ),
                    (
                        3,
                        "bugfix",
                        "t3",
                        "no learned marker here at all\n",
                        "repo-a",
                        "2026-07-21 10:00:00",
                        None,
                    ),
                    (
                        4,
                        "bugfix",
                        "t4",
                        "**Learned**: excluded because deleted\n",
                        "repo-a",
                        "2026-07-21 10:00:00",
                        "2026-08-01 00:00:00",
                    ),
                ],
            )

            uri = "file:{}?mode=ro".format(db_path)
            con = sqlite3.connect(uri, uri=True)
            try:
                mine.verify_schema(con)
                lessons = mine.read_lessons(con)
            finally:
                con.close()

            self.assertEqual(len(lessons), 1)
            self.assertEqual(lessons[0].obs_id, 1)
            self.assertEqual(lessons[0].text, "something learned here")


if __name__ == "__main__":
    unittest.main()
