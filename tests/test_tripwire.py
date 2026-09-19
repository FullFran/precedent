"""Tests for the precedent tripwire hook.

Run with: python3 -m unittest discover tests
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "plugin" / "claude-code" / "scripts" / "tripwire.py"


def _load_tripwire_module():
    spec = importlib.util.spec_from_file_location("tripwire", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tripwire = _load_tripwire_module()


def run_hook_subprocess(payload, patterns_dir=None, precedent_home=None, env_overrides=None):
    """Invoke the script as a real subprocess in default (hook) mode."""
    env = dict(os.environ)
    if patterns_dir is not None:
        env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    if precedent_home is not None:
        env["PRECEDENT_HOME"] = str(precedent_home)
    if env_overrides:
        env.update(env_overrides)

    if isinstance(payload, (dict, list)):
        stdin_data = json.dumps(payload)
    else:
        stdin_data = payload

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_stats_subprocess(precedent_home, patterns_dir=None, env_overrides=None):
    env = dict(os.environ)
    env["PRECEDENT_HOME"] = str(precedent_home)
    if patterns_dir is not None:
        env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--stats"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_check_subprocess(patterns_dir, env_overrides=None):
    env = dict(os.environ)
    env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_session_start_subprocess(patterns_dir, env_overrides=None):
    env = dict(os.environ)
    env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--session-start"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_match_subprocess(args, patterns_dir=None, precedent_home=None, env_overrides=None):
    """Invoke the script as a real subprocess in --match mode."""
    env = dict(os.environ)
    if patterns_dir is not None:
        env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    if precedent_home is not None:
        env["PRECEDENT_HOME"] = str(precedent_home)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--match"] + args,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def run_init_subprocess(precedent_home, patterns_dir=None, env_overrides=None):
    env = dict(os.environ)
    env["PRECEDENT_HOME"] = str(precedent_home)
    if patterns_dir is not None:
        env["PRECEDENT_PATTERNS"] = str(patterns_dir)
    else:
        env.pop("PRECEDENT_PATTERNS", None)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--init"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    return result


def edit_payload(file_path):
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path}}


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


GATE_PAGE = """# A gate that cannot fail

**Trigger:** you are adding CI.

**Trigger paths:** `.github/workflows/**` `**/Makefile`

**Class:** the check reports success without having checked.

Body text for the gate pattern.
"""

TESTS_PAGE = """# Tests that read the real environment

**Trigger:** you are writing a test.

**Trigger paths:** `**/*_test.go` `**/*.test.ts` `**/*.spec.ts`

**Class:** the suite passes because of the machine, not the code.

Body text for the environment pattern.
"""

COMMAND_ONLY_PAGE = """# Backticks execute in a shell body

**Trigger:** you are running gh with a body argument.

**Trigger command:** `gh * --body*` `gh * --body-file*`

**Class:** bash executes backticks in command text instead of passing it through.

Body text for the shell pattern.
"""

BOTH_KINDS_PAGE = """# Both trigger kinds

**Trigger:** relevant to both an edit and a command.

**Trigger paths:** `**/deploy.sh`

**Trigger command:** `terraform apply*`

**Class:** carries both trigger lines.

Body text for the dual-trigger pattern.
"""

NO_HEADING_PAGE = """Some prose with no heading at all.

**Trigger paths:** `**/*.rb`
"""

NO_TRIGGER_PAGE = """# A page with no trigger line

Just prose, no machine-readable trigger paths or trigger command line here.
"""

EMPTY_PATH_GLOBS_PAGE = """# A page with an empty trigger paths line

**Trigger paths:**

No globs on that line at all.
"""

INDEX_PAGE = """# Pattern index

Not a real pattern page, must be excluded.

**Trigger paths:** `**/*.everything`
"""


class TempCorpus:
    """Context manager building a temp patterns/ directory."""

    def __init__(self, pages):
        self.pages = pages
        self.dir = None

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="precedent-patterns-"))
        for filename, content in self.pages.items():
            (self.dir / filename).write_text(content, encoding="utf-8")
        return self.dir

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestGlobMatchingDocumentedCases(unittest.TestCase):
    def test_star_matches_within_segment(self):
        self.assertTrue(tripwire.glob_match("*.spec.ts", "app.spec.ts"))

    def test_star_never_crosses_slash(self):
        self.assertFalse(tripwire.glob_match("*.spec.ts", "src/app.spec.ts"))
        self.assertFalse(tripwire.glob_match("*.md", "patterns/index.md"))

    def test_double_star_matches_bare_file(self):
        self.assertTrue(tripwire.glob_match("**/Makefile", "Makefile"))

    def test_double_star_matches_nested_file(self):
        self.assertTrue(tripwire.glob_match("**/Makefile", "sub/dir/Makefile"))

    def test_double_star_zero_segments_with_star_suffix(self):
        self.assertTrue(tripwire.glob_match("**/*.spec.ts", "app.spec.ts"))
        self.assertTrue(tripwire.glob_match("**/*.spec.ts", "src/app.spec.ts"))

    def test_anchored_directory_glob(self):
        self.assertTrue(
            tripwire.glob_match(".github/workflows/**", ".github/workflows/ci.yml")
        )
        self.assertFalse(
            tripwire.glob_match(".github/workflows/**", ".github/other/ci.yml")
        )
        # '**' matches zero or more whole segments (same rule that makes
        # '**/Makefile' match a bare 'Makefile'), so the trailing '**' here
        # also matches zero segments: the bare directory itself matches too.
        self.assertTrue(
            tripwire.glob_match(".github/workflows/**", ".github/workflows")
        )

    def test_go_test_glob(self):
        self.assertTrue(
            tripwire.glob_match("**/*_test.go", "internal/glob/glob_test.go")
        )
        self.assertFalse(tripwire.glob_match("**/*_test.go", "internal/glob/glob.go"))

    def test_exact_segment_is_not_substring_match(self):
        self.assertFalse(tripwire.glob_match("**/Makefile", "Makefile2"))
        self.assertFalse(tripwire.glob_match("**/Makefile", "NotAMakefile"))

    def test_no_match_unrelated_path(self):
        self.assertFalse(tripwire.glob_match("**/Makefile", "README.md"))


class TestCollapseDoubleStar(unittest.TestCase):
    def test_collapses_long_run(self):
        segs = ["**"] * 8 + ["never.xyz"]
        self.assertEqual(tripwire.collapse_double_star(segs), ["**", "never.xyz"])

    def test_leaves_single_double_star(self):
        self.assertEqual(tripwire.collapse_double_star(["**", "foo"]), ["**", "foo"])

    def test_leaves_non_adjacent_double_stars(self):
        segs = ["**", "a", "**", "b"]
        self.assertEqual(tripwire.collapse_double_star(segs), segs)

    def test_no_double_star_unaffected(self):
        segs = ["a", "b", "c"]
        self.assertEqual(tripwire.collapse_double_star(segs), segs)


class TestPathologicalGlob(unittest.TestCase):
    def test_consecutive_double_star_stays_bounded(self):
        # 8 runs of '**' against a 40-segment path that does NOT match,
        # forcing full exploration of the failure space.
        pattern = "/".join(["**"] * 8 + ["never.xyz"])
        path = "/".join("seg{}".format(i) for i in range(40))

        start = time.monotonic()
        result = tripwire.glob_match(pattern, path)
        elapsed = time.monotonic() - start

        self.assertFalse(result)
        self.assertLess(elapsed, 1.0, "glob_match took {:.3f}s, expected well under 1s".format(elapsed))

    def test_end_to_end_hook_pathological_case(self):
        trigger_line = "**Trigger paths:** " + "`" + "/".join(["**"] * 8 + ["never.xyz"]) + "`"
        page = "# Pathological pattern\n\n{}\n\nBody.\n".format(trigger_line)

        with TempCorpus({"pathological.md": page}) as patterns_dir:
            long_path = "/".join("segment{}".format(i) for i in range(40))
            with tempfile.TemporaryDirectory() as home:
                start = time.monotonic()
                result = run_hook_subprocess(
                    edit_payload(long_path), patterns_dir=patterns_dir, precedent_home=home
                )
                elapsed = time.monotonic() - start

            self.assertEqual(result.returncode, 0)
            self.assertLess(elapsed, 5.0)


class TestCommandGlobMatching(unittest.TestCase):
    def test_matches_body_flag_with_intervening_text(self):
        self.assertTrue(
            tripwire.command_glob_match(
                "gh * --body*", 'gh issue comment --body "see `owner/repo#1`"'
            )
        )

    def test_star_matches_spaces_and_slashes(self):
        self.assertTrue(
            tripwire.command_glob_match(
                "gh * --body*", "gh pr create --repo owner/repo --body 'text here'"
            )
        )

    def test_body_file_variant(self):
        self.assertTrue(
            tripwire.command_glob_match(
                "gh * --body-file*", "gh issue create --body-file /tmp/x.md"
            )
        )

    def test_no_match_unrelated_command(self):
        self.assertFalse(tripwire.command_glob_match("gh * --body*", "ls -la"))

    def test_no_match_when_flag_absent(self):
        self.assertFalse(
            tripwire.command_glob_match("gh * --body*", "gh issue list --state open")
        )

    def test_exact_literal_no_star(self):
        self.assertTrue(tripwire.command_glob_match("ls -la", "ls -la"))
        self.assertFalse(tripwire.command_glob_match("ls -la", "ls -la extra"))

    def test_there_is_no_double_star_special_case(self):
        # Unlike the path matcher, '**' has no special meaning here: it is
        # just two adjacent single-character wildcards.
        self.assertTrue(tripwire.command_glob_match("gh**body", "gh issue --body"))


class TestPageParsing(unittest.TestCase):
    def test_parses_title_and_path_globs(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(GATE_PAGE)
        self.assertEqual(title, "A gate that cannot fail")
        self.assertEqual(path_globs, [".github/workflows/**", "**/Makefile"])
        self.assertEqual(command_globs, [])
        self.assertIsNone(reason)

    def test_parses_title_and_command_globs(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(COMMAND_ONLY_PAGE)
        self.assertEqual(title, "Backticks execute in a shell body")
        self.assertEqual(path_globs, [])
        self.assertEqual(command_globs, ["gh * --body*", "gh * --body-file*"])
        self.assertIsNone(reason)

    def test_parses_both_kinds_on_one_page(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(BOTH_KINDS_PAGE)
        self.assertEqual(title, "Both trigger kinds")
        self.assertEqual(path_globs, ["**/deploy.sh"])
        self.assertEqual(command_globs, ["terraform apply*"])
        self.assertIsNone(reason)

    def test_page_with_no_heading_is_skipped(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(NO_HEADING_PAGE)
        self.assertIsNone(title)
        self.assertIsNotNone(reason)

    def test_page_with_neither_trigger_line_is_skipped(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(NO_TRIGGER_PAGE)
        self.assertEqual(title, "A page with no trigger line")
        self.assertEqual(path_globs, [])
        self.assertEqual(command_globs, [])
        self.assertIsNotNone(reason)

    def test_page_with_empty_trigger_paths_line_is_skipped(self):
        title, path_globs, command_globs, reason = tripwire.parse_page(EMPTY_PATH_GLOBS_PAGE)
        self.assertEqual(title, "A page with an empty trigger paths line")
        self.assertEqual(path_globs, [])
        self.assertEqual(command_globs, [])
        self.assertIsNotNone(reason)


class TestLoadCorpus(unittest.TestCase):
    def test_index_excluded_and_pages_loaded(self):
        with TempCorpus(
            {
                "index.md": INDEX_PAGE,
                "gate.md": GATE_PAGE,
                "tests.md": TESTS_PAGE,
            }
        ) as patterns_dir:
            pages, skipped = tripwire.load_corpus(patterns_dir)
            names = sorted(p.filename for p in pages)
            self.assertEqual(names, ["gate.md", "tests.md"])
            self.assertEqual(skipped, [])

    def test_broken_pages_are_reported_as_skipped(self):
        with TempCorpus(
            {
                "good.md": GATE_PAGE,
                "no-heading.md": NO_HEADING_PAGE,
                "no-trigger.md": NO_TRIGGER_PAGE,
            }
        ) as patterns_dir:
            pages, skipped = tripwire.load_corpus(patterns_dir)
            self.assertEqual([p.filename for p in pages], ["good.md"])
            skipped_names = {name for name, _reason in skipped}
            self.assertEqual(skipped_names, {"no-heading.md", "no-trigger.md"})

    def test_missing_directory_yields_empty_results(self):
        pages, skipped = tripwire.load_corpus(Path("/nonexistent/precedent/patterns/dir"))
        self.assertEqual(pages, [])
        self.assertEqual(skipped, [])

    def test_command_only_page_loads_with_empty_path_globs(self):
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            pages, skipped = tripwire.load_corpus(patterns_dir)
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].path_globs, [])
            self.assertEqual(pages[0].command_globs, ["gh * --body*", "gh * --body-file*"])
            self.assertEqual(skipped, [])


class TestHookEndToEndPath(unittest.TestCase):
    def test_hit_produces_exact_output_shape(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        stdout = result.stdout.strip()
        self.assertTrue(stdout, "expected output on a hit")
        parsed = json.loads(stdout)
        self.assertEqual(
            parsed,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": GATE_PAGE,
                }
            },
        )

    def test_write_tool_also_matches(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                payload = {"tool_name": "Write", "tool_input": {"file_path": "Makefile"}}
                result = run_hook_subprocess(payload, patterns_dir=patterns_dir, precedent_home=home)

        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.stdout.strip())
        self.assertEqual(parsed["hookSpecificOutput"]["additionalContext"], GATE_PAGE)

    def test_miss_produces_no_output(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_hook_subprocess(
                    edit_payload("src/unrelated.py"), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_edit_surfaces_path_page_not_command_only_page(self):
        with TempCorpus({"gate.md": GATE_PAGE, "shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.stdout.strip())
        # Only the path-triggered page shows up; the command-only page has
        # no path globs at all, so it cannot match an Edit.
        self.assertEqual(parsed["hookSpecificOutput"]["additionalContext"], GATE_PAGE)

    def test_hit_and_miss_both_log(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )
                run_hook_subprocess(
                    edit_payload("src/unrelated.py"), patterns_dir=patterns_dir, precedent_home=home
                )

                log_path = Path(home) / "tripwire.jsonl"
                self.assertTrue(log_path.is_file())
                lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]

        self.assertEqual(len(lines), 2)
        hit_entry, miss_entry = lines
        self.assertEqual(hit_entry["kind"], "path")
        self.assertEqual(hit_entry["subject"], "Makefile")
        self.assertEqual(hit_entry["count"], 1)
        self.assertEqual(hit_entry["matched"], ["A gate that cannot fail"])
        self.assertIn("ts", hit_entry)

        self.assertEqual(miss_entry["kind"], "path")
        self.assertEqual(miss_entry["subject"], "src/unrelated.py")
        self.assertEqual(miss_entry["count"], 0)
        self.assertEqual(miss_entry["matched"], [])

    def test_unwritable_log_location_does_not_raise_or_change_output(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as tmp:
                # Create a plain FILE where the log's parent directory would
                # need to be, so mkdir(parents=True) fails deterministically
                # without needing root or chmod tricks.
                blocker = Path(tmp) / "blocked"
                blocker.write_text("not a directory", encoding="utf-8")
                fake_home = blocker / "precedent-home"

                result = run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=fake_home
                )

        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.stdout.strip())
        self.assertEqual(
            parsed["hookSpecificOutput"]["additionalContext"], GATE_PAGE
        )


class TestHookEndToEndCommand(unittest.TestCase):
    def test_bash_hit_produces_exact_output_shape(self):
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                command = 'gh issue comment --body "see `owner/repo#1`"'
                result = run_hook_subprocess(
                    bash_payload(command), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        stdout = result.stdout.strip()
        self.assertTrue(stdout, "expected output on a hit")
        parsed = json.loads(stdout)
        self.assertEqual(
            parsed,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": COMMAND_ONLY_PAGE,
                }
            },
        )

    def test_bash_miss_produces_no_output(self):
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_hook_subprocess(
                    bash_payload("ls -la"), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_bash_command_does_not_match_path_only_page(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_hook_subprocess(
                    bash_payload("make -f Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class TestUnrecognisedToolName(unittest.TestCase):
    def test_unknown_tool_name_does_nothing(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                payload = {"tool_name": "Read", "tool_input": {"file_path": "Makefile"}}
                result = run_hook_subprocess(payload, patterns_dir=patterns_dir, precedent_home=home)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_missing_tool_name_does_nothing(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                payload = {"tool_input": {"file_path": "Makefile"}}
                result = run_hook_subprocess(payload, patterns_dir=patterns_dir, precedent_home=home)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unknown_tool_name_still_exits_zero_with_no_log(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                payload = {"tool_name": "Read", "tool_input": {"file_path": "Makefile"}}
                run_hook_subprocess(payload, patterns_dir=patterns_dir, precedent_home=home)
                log_path = Path(home) / "tripwire.jsonl"
                self.assertFalse(log_path.exists())


class TestCommandLogging(unittest.TestCase):
    def test_command_subject_is_reduced_to_leading_words(self):
        command = "gh issue comment --body 'short'"
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    bash_payload(command), patterns_dir=patterns_dir, precedent_home=home
                )
                entry = json.loads((Path(home) / "tripwire.jsonl").read_text("utf-8").strip())
        self.assertEqual(entry["kind"], "command")
        self.assertEqual(entry["subject"], "gh issue comment")
        self.assertEqual(entry["matched"], ["Backticks execute in a shell body"])

    def test_secret_past_a_length_limit_never_reaches_the_log(self):
        # Truncating to a character count is not redaction; it protects only
        # by accident of position. This header sits inside the first 80
        # characters and a length limit wrote it out in full.
        command = ('gh issue comment --body "see x" && '
                   'curl -H "Authorization: Bearer sk-LEAKED"')
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    bash_payload(command), patterns_dir=patterns_dir, precedent_home=home
                )
                raw = (Path(home) / "tripwire.jsonl").read_text("utf-8")
        self.assertEqual(json.loads(raw.strip())["subject"], "gh issue comment")
        for needle in ("Authorization", "Bearer", "sk-LEAKED"):
            self.assertNotIn(needle, raw)

    def test_secret_in_a_leading_env_assignment_never_reaches_the_log(self):
        # The case a length limit gets exactly backwards: the token is the
        # first word, so every prefix of the command contains it.
        command = "TOKEN=sk-LEAKED gh issue comment --body 'x'"
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    bash_payload(command), patterns_dir=patterns_dir, precedent_home=home
                )
                raw = (Path(home) / "tripwire.jsonl").read_text("utf-8")
        self.assertEqual(json.loads(raw.strip())["subject"], "(redacted)")
        for needle in ("TOKEN", "sk-LEAKED"):
            self.assertNotIn(needle, raw)

    def test_what_survives_still_answers_the_question_the_log_exists_for(self):
        # It has to be enough to tell a too-narrow glob from a quiet one,
        # which means the program and its subcommand and nothing else.
        self.assertEqual(
            tripwire.command_log_subject("gh auth login --with-token < /dev/stdin"),
            "gh auth login",
        )
        self.assertEqual(
            tripwire.command_log_subject("AWS_SECRET_ACCESS_KEY=abc aws s3 cp a b"),
            "(redacted)",
        )
        self.assertEqual(tripwire.command_log_subject(""), "(redacted)")

    def test_path_log_entry_kind_is_path_and_subject_untruncated(self):
        long_path = "src/" + ("a" * 200) + "/Makefile"
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    edit_payload(long_path), patterns_dir=patterns_dir, precedent_home=home
                )
                log_path = Path(home) / "tripwire.jsonl"
                entry = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertEqual(entry["kind"], "path")
        self.assertEqual(entry["subject"], long_path)


class TestSafetyMatrix(unittest.TestCase):
    def setUp(self):
        self.patterns_ctx = TempCorpus({"gate.md": GATE_PAGE, "shell.md": COMMAND_ONLY_PAGE})
        self.patterns_dir = self.patterns_ctx.__enter__()
        self.home_dir = tempfile.mkdtemp(prefix="precedent-home-")

    def tearDown(self):
        self.patterns_ctx.__exit__(None, None, None)
        shutil.rmtree(self.home_dir, ignore_errors=True)

    def test_malformed_payload_exits_zero(self):
        result = run_hook_subprocess("{not valid json at all", patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_empty_payload_exits_zero(self):
        result = run_hook_subprocess("", patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_bash_payload_with_no_command_exits_zero(self):
        payload = {"tool_name": "Bash", "tool_input": {}}
        result = run_hook_subprocess(payload, patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_edit_payload_with_no_file_path_exits_zero(self):
        payload = {"tool_name": "Edit", "tool_input": {}}
        result = run_hook_subprocess(payload, patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unknown_tool_name_exits_zero(self):
        payload = {"tool_name": "SomeOtherTool", "tool_input": {"file_path": "Makefile", "command": "ls"}}
        result = run_hook_subprocess(payload, patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_payload_with_path_literally_dashdash_stats(self):
        # tool_input.file_path == "--stats" must be treated as an ordinary
        # (non-matching) path, never as a mode switch: mode switching reads
        # sys.argv, not the JSON payload.
        result = run_hook_subprocess(edit_payload("--stats"), patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_five_megabyte_payload_exits_zero(self):
        padding = "x" * (5 * 1024 * 1024)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "src/unrelated.py", "old_string": padding}}
        result = run_hook_subprocess(payload, patterns_dir=self.patterns_dir, precedent_home=self.home_dir)
        self.assertEqual(result.returncode, 0)

    def test_five_megabyte_command_string_exits_zero(self):
        command = "gh issue comment --body " + ("x" * (5 * 1024 * 1024))
        result = run_hook_subprocess(
            bash_payload(command), patterns_dir=self.patterns_dir, precedent_home=self.home_dir
        )
        self.assertEqual(result.returncode, 0)

    def test_unwritable_home_exits_zero(self):
        unwritable = Path(tempfile.mkdtemp(prefix="precedent-unwritable-"))
        os.chmod(unwritable, 0o500)
        try:
            result = run_hook_subprocess(
                edit_payload("Makefile"),
                patterns_dir=self.patterns_dir,
                precedent_home=unwritable / "nested" / "precedent",
            )
            self.assertEqual(result.returncode, 0)
        finally:
            os.chmod(unwritable, 0o700)
            shutil.rmtree(unwritable, ignore_errors=True)


class TestCheckMode(unittest.TestCase):
    def test_check_shows_both_trigger_kinds_for_every_page(self):
        with TempCorpus(
            {"gate.md": GATE_PAGE, "shell.md": COMMAND_ONLY_PAGE, "both.md": BOTH_KINDS_PAGE}
        ) as patterns_dir:
            result = run_check_subprocess(patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("A gate that cannot fail", result.stdout)
        self.assertIn(".github/workflows/**", result.stdout)
        self.assertIn("Backticks execute in a shell body", result.stdout)
        self.assertIn("gh * --body*", result.stdout)
        self.assertIn("Both trigger kinds", result.stdout)
        self.assertIn("**/deploy.sh", result.stdout)
        self.assertIn("terraform apply*", result.stdout)
        self.assertIn("trigger paths:", result.stdout)
        self.assertIn("trigger command:", result.stdout)
        self.assertIn("(none)", result.stdout)  # gate.md has no command globs


class TestSessionStartMode(unittest.TestCase):
    """--session-start (the SessionStart hook). See run_session_start()'s
    docstring: plain stdout from SessionStart DOES reach the model, so a
    healthy install (a corpus with at least one usable page) must print
    nothing at all, and only an install with nothing to surface prints its
    one notice."""

    def test_corpus_with_a_page_produces_no_output_at_all(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            result = run_session_start_subprocess(patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_missing_corpus_directory_prints_notice_naming_it(self):
        with tempfile.TemporaryDirectory() as home:
            missing = Path(home) / "nonexistent" / "patterns"
            result = run_session_start_subprocess(patterns_dir=missing)

        self.assertEqual(result.returncode, 0)
        self.assertIn(str(missing), result.stdout)

    def test_corpus_with_no_usable_pages_also_prints_notice(self):
        # A single page with no trigger line is loaded by nothing: as far as
        # the hook is concerned this is as silent as a missing corpus, so it
        # gets the same notice.
        with TempCorpus({"no-trigger.md": NO_TRIGGER_PAGE}) as patterns_dir:
            result = run_session_start_subprocess(patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn(str(patterns_dir), result.stdout)

    def test_notice_mentions_init_and_precedent_patterns(self):
        with tempfile.TemporaryDirectory() as home:
            missing = Path(home) / "nonexistent" / "patterns"
            result = run_session_start_subprocess(patterns_dir=missing)

        self.assertIn("--init", result.stdout)
        self.assertIn("PRECEDENT_PATTERNS", result.stdout)

    def test_unreadable_corpus_directory_exits_zero_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as home:
            patterns_dir = Path(home) / "patterns"
            patterns_dir.mkdir()
            (patterns_dir / "gate.md").write_text(GATE_PAGE, encoding="utf-8")
            os.chmod(patterns_dir, 0o000)
            try:
                result = run_session_start_subprocess(patterns_dir=patterns_dir)
            finally:
                os.chmod(patterns_dir, 0o700)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


class TestDefaultCorpusResolution(unittest.TestCase):
    """get_patterns_dir()'s resolution order. Every other test in this file
    sets PRECEDENT_PATTERNS explicitly, so this is the only coverage of what
    happens when it (and PRECEDENT_HOME) are left unset."""

    def setUp(self):
        self._saved = {
            key: os.environ.pop(key, None)
            for key in ("PRECEDENT_PATTERNS", "PRECEDENT_HOME")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_precedent_home_sets_patterns_dir_when_patterns_unset(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ["PRECEDENT_HOME"] = home
            self.assertEqual(tripwire.get_patterns_dir(), Path(home) / "patterns")

    def test_both_unset_falls_back_to_users_home(self):
        # setUp already removed both PRECEDENT_PATTERNS and PRECEDENT_HOME.
        self.assertNotIn("PRECEDENT_PATTERNS", os.environ)
        self.assertNotIn("PRECEDENT_HOME", os.environ)
        self.assertEqual(
            tripwire.get_patterns_dir(), Path.home() / ".precedent" / "patterns"
        )

    def test_precedent_patterns_wins_over_precedent_home_and_default(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as explicit:
            os.environ["PRECEDENT_HOME"] = home
            os.environ["PRECEDENT_PATTERNS"] = explicit
            self.assertEqual(tripwire.get_patterns_dir(), Path(explicit))
            # PRECEDENT_HOME alone (no PRECEDENT_PATTERNS) would have pointed
            # here instead -- confirms PRECEDENT_PATTERNS actually overrode it
            # rather than merely agreeing with it by coincidence.
            self.assertNotEqual(Path(explicit), Path(home) / "patterns")


class TestInitMode(unittest.TestCase):
    def test_creates_directory_and_writes_starter_page(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_init_subprocess(precedent_home=home)
            self.assertEqual(result.returncode, 0)

            patterns_dir = Path(home) / "patterns"
            self.assertTrue(patterns_dir.is_dir())
            starter = patterns_dir / "a-gate-that-cannot-fail.md"
            self.assertTrue(starter.is_file())
            self.assertTrue(starter.read_text(encoding="utf-8").startswith("# "))

        self.assertIn("wrote a-gate-that-cannot-fail.md", result.stdout)
        self.assertIn(str(patterns_dir), result.stdout)

    def test_idempotent_does_not_overwrite_existing_corpus(self):
        with tempfile.TemporaryDirectory() as home:
            first = run_init_subprocess(precedent_home=home)
            self.assertEqual(first.returncode, 0)

            starter = Path(home) / "patterns" / "a-gate-that-cannot-fail.md"
            original_content = starter.read_text(encoding="utf-8")
            original_mtime = starter.stat().st_mtime_ns

            second = run_init_subprocess(precedent_home=home)

            self.assertEqual(second.returncode, 0)
            self.assertIn("already holds", second.stdout)
            self.assertIn("nothing written", second.stdout)
            self.assertEqual(starter.read_text(encoding="utf-8"), original_content)
            self.assertEqual(starter.stat().st_mtime_ns, original_mtime)

    def test_idempotent_also_leaves_a_different_existing_page_alone(self):
        # Idempotent means "don't touch an existing corpus", not just "don't
        # overwrite the exact starter filename" -- a corpus that already has
        # ANY page in it should be left alone.
        with tempfile.TemporaryDirectory() as home:
            patterns_dir = Path(home) / "patterns"
            patterns_dir.mkdir(parents=True)
            (patterns_dir / "my-own-page.md").write_text(GATE_PAGE, encoding="utf-8")

            result = run_init_subprocess(precedent_home=home)

            self.assertEqual(result.returncode, 0)
            self.assertIn("already holds", result.stdout)
            self.assertNotIn("wrote", result.stdout)
            self.assertFalse((patterns_dir / "a-gate-that-cannot-fail.md").exists())

    def test_starter_page_is_parsed_by_the_loader_with_a_trigger_glob(self):
        with tempfile.TemporaryDirectory() as home:
            run_init_subprocess(precedent_home=home)
            patterns_dir = Path(home) / "patterns"
            pages, skipped = tripwire.load_corpus(patterns_dir)

        self.assertEqual(skipped, [])
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertTrue(page.title)
        self.assertGreaterEqual(len(page.path_globs) + len(page.command_globs), 1)

    def test_unwritable_target_reports_problem_and_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A plain FILE where the home directory would need to be, so
            # mkdir(parents=True) fails deterministically without needing
            # root or chmod tricks (same technique the existing unwritable
            # log test uses).
            blocker = Path(tmp) / "blocked"
            blocker.write_text("not a directory", encoding="utf-8")
            fake_home = blocker / "precedent-home"

            result = run_init_subprocess(precedent_home=fake_home)

        self.assertEqual(result.returncode, 0)
        self.assertIn("could not create it", result.stdout)
        self.assertIn("PRECEDENT_PATTERNS", result.stdout)


class TestMatchMode(unittest.TestCase):
    """--match: the transport-neutral match mode other agents' adapters
    shell out to, instead of reimplementing matching themselves."""

    def test_match_path_hit_prints_raw_page_no_envelope(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--path", "Makefile"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        # No hookSpecificOutput envelope -- just the page's raw markdown.
        self.assertEqual(result.stdout, GATE_PAGE)
        self.assertNotIn("hookSpecificOutput", result.stdout)

    def test_match_command_hit_prints_raw_page(self):
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                command = 'gh issue comment --body "see `owner/repo#1`"'
                result = run_match_subprocess(
                    ["--command", command], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, COMMAND_ONLY_PAGE)

    def test_match_path_no_match_prints_nothing(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--path", "src/unrelated.py"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_match_command_no_match_prints_nothing(self):
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--command", "ls -la"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_match_agent_lands_in_log(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_match_subprocess(
                    ["--path", "Makefile", "--agent", "opencode"],
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )
                entry = json.loads((Path(home) / "tripwire.jsonl").read_text("utf-8").strip())

        self.assertEqual(entry["agent"], "opencode")
        self.assertEqual(entry["kind"], "path")
        self.assertEqual(entry["subject"], "Makefile")

    def test_match_default_agent_is_claude_code(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_match_subprocess(
                    ["--path", "Makefile"], patterns_dir=patterns_dir, precedent_home=home
                )
                entry = json.loads((Path(home) / "tripwire.jsonl").read_text("utf-8").strip())

        self.assertEqual(entry["agent"], "claude-code")

    def test_match_command_agent_redacts_secret_same_as_hook_mode(self):
        command = "TOKEN=sk-LEAKED gh issue comment --body 'x'"
        with TempCorpus({"shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_match_subprocess(
                    ["--command", command, "--agent", "opencode"],
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )
                raw = (Path(home) / "tripwire.jsonl").read_text("utf-8")

        entry = json.loads(raw.strip())
        self.assertEqual(entry["agent"], "opencode")
        self.assertEqual(entry["subject"], "(redacted)")
        for needle in ("TOKEN", "sk-LEAKED"):
            self.assertNotIn(needle, raw)

    def test_match_neither_path_nor_command_exits_zero(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--agent", "opencode"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_match_both_path_and_command_exits_zero(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--path", "Makefile", "--command", "ls -la"],
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_match_dangling_flag_with_no_value_exits_zero(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--path"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_match_unrecognised_flag_exits_zero(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                result = run_match_subprocess(
                    ["--nonsense", "value"], patterns_dir=patterns_dir, precedent_home=home
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class TestStatsMode(unittest.TestCase):
    def test_missing_log_prints_plain_sentence(self):
        with tempfile.TemporaryDirectory() as home:
            result = run_stats_subprocess(precedent_home=home)
        self.assertEqual(result.returncode, 0)
        self.assertIn("No tripwire log found", result.stdout)
        self.assertIn(home, result.stdout)

    def test_stats_reports_hits_and_misses(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )
                run_hook_subprocess(
                    edit_payload("src/unrelated.py"), patterns_dir=patterns_dir, precedent_home=home
                )
                result = run_stats_subprocess(precedent_home=home, patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("invocations: 2", result.stdout)
        self.assertIn("hits: 1", result.stdout)
        self.assertIn("misses: 1", result.stdout)
        self.assertIn("A gate that cannot fail", result.stdout)
        self.assertIn("src/unrelated.py", result.stdout)

    def test_stats_breaks_down_by_kind(self):
        with TempCorpus({"gate.md": GATE_PAGE, "shell.md": COMMAND_ONLY_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )
                run_hook_subprocess(
                    bash_payload('gh issue comment --body "see `owner/repo#1`"'),
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )
                result = run_stats_subprocess(precedent_home=home, patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("invocations: 2", result.stdout)
        self.assertIn("by kind: path=1 command=1", result.stdout)
        self.assertIn("[path]", result.stdout)
        self.assertIn("[command]", result.stdout)
        self.assertIn("A gate that cannot fail", result.stdout)
        self.assertIn("Backticks execute in a shell body", result.stdout)

    def test_stats_reads_old_path_field_and_new_subject_field(self):
        with tempfile.TemporaryDirectory() as home:
            log_path = Path(home) / "tripwire.jsonl"
            old_entry = {
                "ts": "2026-01-01T00:00:00Z",
                "path": "Makefile",
                "matched": ["A gate that cannot fail"],
                "count": 1,
                "pages": 1,
                "corpus": "/some/corpus",
            }
            new_entry = {
                "ts": "2026-01-02T00:00:00Z",
                "kind": "command",
                "subject": 'gh issue comment --body "see...',
                "matched": ["Backticks execute in a shell body"],
                "count": 1,
                "pages": 1,
                "corpus": "/some/corpus",
            }
            log_path.write_text(
                json.dumps(old_entry) + "\n" + json.dumps(new_entry) + "\n", encoding="utf-8"
            )

            result = run_stats_subprocess(precedent_home=home)

        self.assertEqual(result.returncode, 0)
        self.assertIn("invocations: 2", result.stdout)
        self.assertIn("by kind: path=1 command=1", result.stdout)
        self.assertIn("A gate that cannot fail", result.stdout)
        self.assertIn("Backticks execute in a shell body", result.stdout)
        self.assertIn("Makefile", result.stdout)

    def test_stats_breaks_down_by_agent_when_more_than_one_appears(self):
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                # One claude-code invocation (the default, via hook mode)...
                run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )
                # ...and two opencode invocations, via --match.
                run_match_subprocess(
                    ["--path", "Makefile", "--agent", "opencode"],
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )
                run_match_subprocess(
                    ["--path", "src/unrelated.py", "--agent", "opencode"],
                    patterns_dir=patterns_dir,
                    precedent_home=home,
                )
                result = run_stats_subprocess(precedent_home=home, patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertIn("invocations: 3", result.stdout)
        self.assertIn("by agent: claude-code=1 opencode=2", result.stdout)

    def test_stats_stays_unchanged_when_only_one_agent_appears(self):
        # Every existing install only ever has one agent (claude-code, via
        # the PreToolUse hook) in its log. --stats must read exactly as it
        # did before the agent field existed: no "by agent:" line at all.
        with TempCorpus({"gate.md": GATE_PAGE}) as patterns_dir:
            with tempfile.TemporaryDirectory() as home:
                run_hook_subprocess(
                    edit_payload("Makefile"), patterns_dir=patterns_dir, precedent_home=home
                )
                result = run_stats_subprocess(precedent_home=home, patterns_dir=patterns_dir)

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("by agent:", result.stdout)


if __name__ == "__main__":
    unittest.main()
