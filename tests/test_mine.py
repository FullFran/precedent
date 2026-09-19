"""Tests for tools/mine.py.

Run with: python3 -m unittest discover tests
"""

import os
import sqlite3
import subprocess
import sys
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


def run_mine_subprocess(args):
    """Invoke tools/mine.py as a real subprocess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)] + args,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ),
    )


# Two lessons worded alike, in two different projects: one candidate group,
# observation ids 1 and 2. The ledger fixtures below reject exactly that
# id set.
LEDGER_FIXTURE_ROWS = [
    (
        1,
        "bugfix",
        "t1",
        "**What**: x\n**Learned**: credentials leaked through an unset auth env variable\n",
        "repo-a",
        "2026-07-21 10:00:00",
        None,
    ),
    (
        2,
        "bugfix",
        "t2",
        "**What**: x\n**Learned**: credentials leaked through an unset auth env variable path\n",
        "repo-b",
        "2026-08-28 10:00:00",
        None,
    ),
]


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
        groups, _suppressed = mine.assemble_groups(lessons, 0.25, 1)
        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(len(groups[0]["projects"]), 2)
        self.assertEqual(len(groups[0]["members"]), 2)
        self.assertEqual(len(groups[1]["projects"]), 1)
        self.assertEqual(len(groups[1]["members"]), 3)

    def test_min_projects_filters_out_single_project_groups(self):
        lessons = make_lessons(self.ROWS)
        groups, _suppressed = mine.assemble_groups(lessons, 0.25, 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["projects"]), 2)


class TestDraft(unittest.TestCase):
    def test_draft_contains_verbatim_evidence(self):
        rows = [
            (1, "repo-a", "2026-07-21", "t1", "credentials leaked through an unset auth env variable"),
            (2, "repo-b", "2026-08-28", "t2", "credentials leaked through an unset auth env variable path"),
        ]
        lessons = make_lessons(rows)
        groups, _suppressed = mine.assemble_groups(lessons, 0.25, 2)
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
        groups, _suppressed = mine.assemble_groups(lessons, 0.25, 2)
        self.assertIsNone(mine.find_group(groups, 999))


class TestLedgerParsing(unittest.TestCase):
    def test_parses_the_documented_format(self):
        entries, malformed = mine.parse_ledger(mine.LEDGER_FORMAT)
        self.assertEqual(malformed, [])
        self.assertEqual([e.decision for e in entries], ["rejected", "admitted", "retired"])
        self.assertEqual(entries[0].obs_ids, frozenset({6570, 7523}))
        self.assertEqual(entries[1].obs_ids, frozenset({101, 102}))
        self.assertEqual(entries[2].obs_ids, frozenset())
        self.assertEqual(entries[0].date, "2026-09-19")
        self.assertIn("generic word", entries[0].note)

    def test_prose_and_blank_lines_are_not_malformed(self):
        text = (
            "# Decisions\n"
            "\n"
            "Everything below is append-only.\n"
            "\n"
            "- 2026-09-19 | rejected | miner group | obs: 1,2 | unrelated\n"
        )
        entries, malformed = mine.parse_ledger(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(malformed, [])

    def test_malformed_lines_are_reported_with_a_reason(self):
        text = (
            "- 2026-09-19 | rejected | miner group | obs: 1,2 | fine\n"
            "- not-a-date | rejected | g | obs: 3 | bad date\n"
            "- 2026-09-19 | rejcted | g | obs: 4 | typo in the decision\n"
            "- 2026-09-19 | rejected | g | 5,6 | no obs: prefix\n"
            "- 2026-09-19 | rejected | g | obs: seven | not an id\n"
            "- 2026-09-19 | rejected | too few fields\n"
        )
        entries, malformed = mine.parse_ledger(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual([lineno for lineno, _raw, _why in malformed], [2, 3, 4, 5, 6])
        reasons = " ".join(why for _l, _r, why in malformed)
        self.assertIn("YYYY-MM-DD", reasons)
        self.assertIn("rejcted", reasons)  # the typo is named, not swallowed
        self.assertIn("obs:", reasons)

    def test_only_rejected_entries_with_ids_suppress(self):
        entries, _malformed = mine.parse_ledger(mine.LEDGER_FORMAT)
        rejected = mine.rejected_obs_sets(entries)
        # The admitted and retired lines are read, but they are not
        # suppression: only the rejected one keys anything.
        self.assertEqual(list(rejected), [frozenset({6570, 7523})])

    def test_id_less_rejection_keys_nothing(self):
        entries, _malformed = mine.parse_ledger(
            "- 2026-09-19 | rejected | g | obs: - | no ids at all\n"
        )
        self.assertEqual(mine.rejected_obs_sets(entries), {})


class TestLedgerSuppression(unittest.TestCase):
    ROWS = [
        (1, "repo-a", "2026-07-21", "t1", "credentials leaked through an unset auth env variable"),
        (2, "repo-b", "2026-08-28", "t2", "credentials leaked through an unset auth env variable path"),
    ]

    def _rejected(self, text):
        entries, _malformed = mine.parse_ledger(text)
        return mine.rejected_obs_sets(entries)

    def test_group_with_a_rejected_id_set_is_suppressed(self):
        lessons = make_lessons(self.ROWS)
        rejected = self._rejected(
            "- 2026-09-19 | rejected | miner group | obs: 1,2 | unrelated documents\n"
        )
        groups, suppressed = mine.assemble_groups(lessons, 0.25, 2, rejected)
        self.assertEqual(groups, [])
        self.assertEqual(len(suppressed), 1)
        obs_ids, entry = suppressed[0]
        self.assertEqual(obs_ids, frozenset({1, 2}))
        self.assertEqual(entry.note, "unrelated documents")

    def test_id_order_in_the_ledger_does_not_matter(self):
        lessons = make_lessons(self.ROWS)
        rejected = self._rejected(
            "- 2026-09-19 | rejected | miner group | obs: 2, 1 | reversed and spaced\n"
        )
        groups, suppressed = mine.assemble_groups(lessons, 0.25, 2, rejected)
        self.assertEqual(groups, [])
        self.assertEqual(len(suppressed), 1)

    def test_a_different_id_set_is_not_suppressed(self):
        # Equality, not overlap: a group that gained or lost a member has
        # not been ruled on, and must still be proposed.
        lessons = make_lessons(self.ROWS)
        rejected = self._rejected(
            "- 2026-09-19 | rejected | miner group | obs: 1 | only one of them\n"
        )
        groups, suppressed = mine.assemble_groups(lessons, 0.25, 2, rejected)
        self.assertEqual(len(groups), 1)
        self.assertEqual(suppressed, [])

    def test_admitted_entry_does_not_suppress(self):
        lessons = make_lessons(self.ROWS)
        rejected = self._rejected(
            "- 2026-09-19 | admitted | a-page | obs: 1,2 | became a page\n"
        )
        groups, suppressed = mine.assemble_groups(lessons, 0.25, 2, rejected)
        self.assertEqual(len(groups), 1)
        self.assertEqual(suppressed, [])

    def test_no_ledger_suppresses_nothing(self):
        lessons = make_lessons(self.ROWS)
        groups, suppressed = mine.assemble_groups(lessons, 0.25, 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(suppressed, [])

    def test_report_names_the_count_the_ids_and_the_reason(self):
        lessons = make_lessons(self.ROWS)
        text = "- 2026-09-19 | rejected | miner group | obs: 1,2 | unrelated documents\n"
        entries, malformed = mine.parse_ledger(text)
        groups, suppressed = mine.assemble_groups(
            lessons, 0.25, 2, mine.rejected_obs_sets(entries)
        )
        report = mine.format_ledger_report("decisions.md", entries, suppressed, malformed)
        self.assertIn("decisions.md", report)
        self.assertIn("1 group(s) suppressed", report)
        self.assertIn("obs 1,2", report)
        self.assertIn("unrelated documents", report)

    def test_report_is_printed_even_when_nothing_was_suppressed(self):
        entries, malformed = mine.parse_ledger(
            "- 2026-09-19 | admitted | a-page | obs: 9 | nothing to suppress\n"
        )
        report = mine.format_ledger_report("decisions.md", entries, [], malformed)
        self.assertIn("1 decision(s) read", report)
        self.assertIn("0 group(s) suppressed", report)


class TestDraftRecordsObservationIds(unittest.TestCase):
    def test_draft_carries_a_ledger_line_with_its_obs_ids(self):
        lessons = make_lessons(TestLedgerSuppression.ROWS)
        groups, _suppressed = mine.assemble_groups(lessons, 0.25, 2)
        draft = mine.render_draft(groups[0])
        self.assertIn("obs: 1,2", draft)
        self.assertIn("ledger line for this group", draft)


class TestLedgerCli(unittest.TestCase):
    """--ledger against a real fixture store, through the real CLI."""

    def _db_and_ledger(self, tmp, ledger_text):
        db_path = Path(tmp) / "fixture.db"
        make_fixture_db(db_path, LEDGER_FIXTURE_ROWS)
        ledger_path = Path(tmp) / "decisions.md"
        ledger_path.write_text(ledger_text, encoding="utf-8")
        return db_path, ledger_path

    def test_run_without_ledger_reports_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ledger = self._db_and_ledger(tmp, "")
            result = run_mine_subprocess(["--db", str(db_path), "--min-projects", "2"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("1 candidate group(s) met the thresholds", result.stdout)

    def test_ledger_suppresses_the_group_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, ledger_path = self._db_and_ledger(
                tmp,
                "# Decisions\n"
                "\n"
                "- 2026-09-19 | rejected | miner group | obs: 1,2 | "
                "shared one generic word; unrelated documents\n",
            )
            result = run_mine_subprocess(
                ["--db", str(db_path), "--min-projects", "2", "--ledger", str(ledger_path)]
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("no candidate groups met the thresholds", result.stdout)
        self.assertIn("1 group(s) suppressed", result.stdout)
        self.assertIn("obs 1,2", result.stdout)
        self.assertIn("unrelated documents", result.stdout)

    def test_malformed_ledger_line_is_ignored_without_aborting_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, ledger_path = self._db_and_ledger(
                tmp,
                "- this line is not a ledger entry at all\n"
                "- 2026-09-19 | rejcted | miner group | obs: 1,2 | typo in the decision\n",
            )
            result = run_mine_subprocess(
                ["--db", str(db_path), "--min-projects", "2", "--ledger", str(ledger_path)]
            )

        self.assertEqual(result.returncode, 0)
        # The typo'd decision suppressed nothing, and the run says so
        # instead of quietly behaving as though the line were not there.
        self.assertIn("1 candidate group(s) met the thresholds", result.stdout)
        self.assertIn("0 group(s) suppressed", result.stdout)
        self.assertIn("ignored malformed ledger line", result.stdout)
        self.assertIn("rejcted", result.stdout)

    def test_missing_ledger_file_is_an_error_not_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ledger = self._db_and_ledger(tmp, "")
            missing = Path(tmp) / "nope.md"
            result = run_mine_subprocess(
                ["--db", str(db_path), "--min-projects", "2", "--ledger", str(missing)]
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(str(missing), result.stderr)

    def test_draft_honours_the_ledger_and_keeps_stdout_a_clean_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, ledger_path = self._db_and_ledger(
                tmp, "- 2026-09-19 | admitted | a-page | obs: 9 | unrelated decision\n"
            )
            result = run_mine_subprocess(
                [
                    "--db", str(db_path),
                    "--min-projects", "2",
                    "--ledger", str(ledger_path),
                    "--draft", "1",
                ]
            )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("# TODO: name the class"))
        self.assertIn("obs: 1,2", result.stdout)
        # The ledger's own report goes to stderr, never into the page.
        self.assertIn("decision(s) read", result.stderr)
        self.assertNotIn("decision(s) read", result.stdout)


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
