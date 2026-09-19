#!/usr/bin/env python3
"""precedent tripwire — PreToolUse hook surfacing failure-class pattern pages.

Reads the corpus in patterns/*.md (excluding index.md) directly from disk
and matches it against the tool about to run:

  - Edit / Write: trigger-path globs against tool_input.file_path.
  - Bash:         trigger-command globs against tool_input.command.

A page may carry a `**Trigger paths:**` line, a `**Trigger command:**`
line, both, or neither (in which case it is reported as skipped). Emits
additionalContext for every matching page. No database, no server, no
build step: one stdlib-only script invoked by absolute path through
${CLAUDE_PLUGIN_ROOT}.

Safety contract: this script must always exit 0. It is invoked as a
PreToolUse hook, where an exit code of 2 denies the tool call outright.
Any failure along the way — malformed payload, unreadable corpus,
unwritable log directory, whatever — must be swallowed silently rather
than ever blocking or slowing down an edit or a command.

Usage:
    tripwire.py            hook mode (default): reads the hook JSON payload
                            from stdin, prints hookSpecificOutput JSON on a
                            match, prints nothing on a miss.
    tripwire.py --match --path <path>       transport-neutral match mode:
    tripwire.py --match --command <command> answers the matching question
                            directly (no hookSpecificOutput envelope), for
                            an adapter belonging to an agent other than
                            Claude Code. Takes an optional --agent <name>
                            (default "claude-code"), recorded in the log.
    tripwire.py --stats     reads the telemetry log back and prints a report.
    tripwire.py --check     reports what corpus is loaded and each page's
                            trigger globs, of both kinds.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HEADING_PREFIX = "# "
TRIGGER_PATH_LINE_PREFIX = "**Trigger paths:**"
TRIGGER_COMMAND_LINE_PREFIX = "**Trigger command:**"

# How many leading words of a command line reach the log. See
# command_log_subject() for why it is words and not characters.
COMMAND_LOG_WORDS = 3


# ---------------------------------------------------------------------------
# Corpus + log locations
# ---------------------------------------------------------------------------

def get_home_dir():
    """The directory holding a user's corpus and telemetry log.

    PRECEDENT_HOME overrides it; otherwise ~/.precedent.
    """
    override = os.environ.get("PRECEDENT_HOME")
    return Path(override) if override else (Path.home() / ".precedent")


def get_patterns_dir():
    """Resolve the corpus directory.

    PRECEDENT_PATTERNS wins, otherwise <home>/patterns.

    The corpus deliberately does NOT live beside this script. This tool ships
    the mechanism; the pattern pages are the user's own writing, about their
    own work, and belong to them. Resolving the corpus relative to the script
    also does not survive installation: a Claude Code marketplace install
    copies only plugin/claude-code/, so a sibling directory never arrives and
    the hook would run, find nothing, exit 0 and do nothing — silently, and
    looking exactly like an honest miss.

    Point PRECEDENT_PATTERNS at a corpus kept anywhere, or symlink
    ~/.precedent/patterns at it. Run --check to see which one is in use.
    """
    override = os.environ.get("PRECEDENT_PATTERNS")
    if override:
        return Path(override)
    return get_home_dir() / "patterns"


def get_log_path():
    """Resolve the telemetry log path. See get_home_dir()."""
    return get_home_dir() / "tripwire.jsonl"


# ---------------------------------------------------------------------------
# Corpus loading + page parsing
# ---------------------------------------------------------------------------

class Page:
    __slots__ = ("title", "path_globs", "command_globs", "content", "filename")

    def __init__(self, title, path_globs, command_globs, content, filename):
        self.title = title
        self.path_globs = path_globs
        self.command_globs = command_globs
        self.content = content
        self.filename = filename


def parse_page(text):
    """Parse one pattern page's markdown text.

    Returns (title, path_globs, command_globs, skip_reason). skip_reason is
    None when a title was found and at least one of the two trigger lines
    contributed at least one backtick-delimited glob; otherwise the fields
    that were found are still returned and skip_reason explains why the
    page is unusable as a whole.

    The two trigger lines are independent: a page may carry either, both,
    or neither. Carrying neither is what makes it skipped.
    """
    title = None
    path_globs = []
    command_globs = []
    path_line_seen = False
    command_line_seen = False

    for line in text.splitlines():
        if title is None and line.startswith(HEADING_PREFIX):
            title = line[len(HEADING_PREFIX):].strip()
        if line.startswith(TRIGGER_PATH_LINE_PREFIX):
            path_line_seen = True
            path_globs = re.findall(r"`([^`]+)`", line)
        if line.startswith(TRIGGER_COMMAND_LINE_PREFIX):
            command_line_seen = True
            command_globs = re.findall(r"`([^`]+)`", line)

    if title is None:
        return None, [], [], "no '# ' heading found"

    if not path_line_seen and not command_line_seen:
        return (
            title,
            [],
            [],
            "no '**Trigger paths:**' or '**Trigger command:**' line found",
        )

    if path_globs or command_globs:
        return title, path_globs, command_globs, None

    # At least one trigger line was seen, but neither yielded a usable glob.
    if path_line_seen and not command_line_seen:
        reason = "'**Trigger paths:**' line has no backtick-delimited globs"
    elif command_line_seen and not path_line_seen:
        reason = "'**Trigger command:**' line has no backtick-delimited globs"
    else:
        reason = "trigger lines present but neither has a backtick-delimited glob"
    return title, [], [], reason


def load_corpus(patterns_dir):
    """Load every usable pattern page under patterns_dir.

    Returns (pages, skipped) where skipped is a list of (filename, reason)
    for pages that could not be parsed or read. index.md is always excluded.
    Never raises: a missing or unreadable directory yields empty results.
    """
    pages = []
    skipped = []

    try:
        entries = sorted(patterns_dir.glob("*.md"))
    except OSError:
        return pages, skipped

    for entry in entries:
        if entry.name == "index.md":
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append((entry.name, "unreadable: {}".format(exc)))
            continue

        title, path_globs, command_globs, reason = parse_page(text)
        if reason is not None:
            skipped.append((entry.name, reason))
            continue

        pages.append(
            Page(
                title=title,
                path_globs=path_globs,
                command_globs=command_globs,
                content=text,
                filename=entry.name,
            )
        )

    return pages, skipped


# ---------------------------------------------------------------------------
# Path glob matching
#
# '*' matches within a single path segment and never crosses '/'.
# '**' matches zero or more whole segments, so '**/Makefile' matches a bare
# 'Makefile'.
#
# Two things keep this bounded on every edit:
#   1. Consecutive '**' runs collapse to a single '**' before matching.
#   2. (pattern-index, path-index) pairs that failed are memoised, so the
#      search is O(pattern_len * path_len) instead of the combinatorial
#      blow-up (on the order of C(d+k, k) branches) that unmemoised
#      backtracking hits with k runs of '**' against a d-segment path.
#
# This matcher is unchanged by the addition of command triggers below: a
# command line has no path segments, '*' there matches anything including
# spaces and slashes, and there is no '**', so it gets its own, simpler
# matcher rather than reusing this one.
# ---------------------------------------------------------------------------

_SEGMENT_REGEX_CACHE = {}


def _segment_regex(segment):
    parts = segment.split("*")
    pattern = "[^/]*".join(re.escape(p) for p in parts)
    return re.compile("^" + pattern + "$")


def _segment_matches(pattern_seg, target_seg):
    if pattern_seg == target_seg:
        return True
    if "*" not in pattern_seg:
        return False
    rx = _SEGMENT_REGEX_CACHE.get(pattern_seg)
    if rx is None:
        rx = _segment_regex(pattern_seg)
        _SEGMENT_REGEX_CACHE[pattern_seg] = rx
    return rx.match(target_seg) is not None


def collapse_double_star(segments):
    """Collapse runs of consecutive '**' segments into a single '**'."""
    collapsed = []
    for seg in segments:
        if seg == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(seg)
    return collapsed


def glob_match(pattern, path):
    """Return True if the trigger path glob `pattern` matches `path`."""
    pattern_segs = collapse_double_star(pattern.split("/"))
    path_segs = path.split("/")
    failed = set()

    def rec(pi, ti):
        if (pi, ti) in failed:
            return False

        if pi == len(pattern_segs):
            ok = ti == len(path_segs)
        elif pattern_segs[pi] == "**":
            ok = rec(pi + 1, ti) or (ti < len(path_segs) and rec(pi, ti + 1))
        else:
            ok = (
                ti < len(path_segs)
                and _segment_matches(pattern_segs[pi], path_segs[ti])
                and rec(pi + 1, ti + 1)
            )

        if not ok:
            failed.add((pi, ti))
        return ok

    return rec(0, 0)


def match_path_pages(pages, file_path):
    """Return the list of pages whose trigger path globs cover file_path."""
    matched = []
    for page in pages:
        for g in page.path_globs:
            if glob_match(g, file_path):
                matched.append(page)
                break
    return matched


# ---------------------------------------------------------------------------
# Command glob matching
#
# A different, simpler matcher from the path one above, and deliberately
# not sharing code with it: a command line has no path segments to reason
# about. A single '*' matches any run of characters, including spaces and
# slashes, and there is no '**' — one wildcard kind is all a whole-string
# match needs. Each pattern compiles to a regex once and the compiled
# pattern is cached, so repeated matching against many commands stays
# cheap without needing the path matcher's segment-pair memoisation.
# ---------------------------------------------------------------------------

_COMMAND_REGEX_CACHE = {}


def _command_regex(pattern):
    parts = pattern.split("*")
    return re.compile("^" + ".*".join(re.escape(p) for p in parts) + "$", re.DOTALL)


def command_glob_match(pattern, command):
    """Return True if the trigger command glob `pattern` matches the whole
    `command` string."""
    rx = _COMMAND_REGEX_CACHE.get(pattern)
    if rx is None:
        rx = _command_regex(pattern)
        _COMMAND_REGEX_CACHE[pattern] = rx
    return rx.match(command) is not None


def match_command_pages(pages, command):
    """Return the list of pages whose trigger command globs cover command."""
    matched = []
    for page in pages:
        for g in page.command_globs:
            if command_glob_match(g, command):
                matched.append(page)
                break
    return matched


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

def command_log_subject(command):
    """Reduce a command line to the few leading words that are safe to keep.

    Truncating to a character count is NOT redaction, and treating it as
    such is how a secret ends up on disk: it protects only by accident of
    position. `gh issue comment --body "..." && curl -H "Authorization:
    Bearer sk-..."` keeps the Authorization header inside the first 80
    characters, while `TOKEN=sk-abc gh issue comment` puts the token in
    the first word. Both were written in full by a length limit.

    Secrets live in arguments, not in program names, so only the leading
    words are kept: tokens are dropped once one begins with "-" or
    contains "=", which covers flags and environment assignments alike.
    What survives is enough to answer the question the log exists for,
    which is whether a command glob is too narrow ("many gh pr misses,
    widen it"), and carries nothing worth leaking.
    """
    kept = []
    for token in str(command).split():
        if token.startswith("-") or "=" in token:
            break
        kept.append(token)
        if len(kept) >= COMMAND_LOG_WORDS:
            break
    if not kept:
        return "(redacted)"
    return " ".join(kept)


def log_event(subject, kind, matched_titles, pages_loaded, corpus_dir, agent="claude-code"):
    """Append one JSONL line for this invocation. Best effort, never raises.

    `subject` is what was checked: a file path for kind "path", or a
    command for kind "command". A command line routinely carries
    credentials, tokens and personal data that a file path does not, so a
    command subject is reduced by command_log_subject() to its leading
    words before it is ever written to disk. A path subject is written in
    full, unchanged.

    pages_loaded and corpus are not decoration. Without them a run that
    found no corpus at all is written exactly like a run that read the
    corpus and matched nothing, and those two call for opposite responses:
    fix the install, or widen a glob. Installing this plugin from a
    marketplace copies only plugin/claude-code/, so the sibling patterns/
    directory does not travel with it and PRECEDENT_PATTERNS has to say
    where it lives. When that is unset the hook runs, exits 0 and does
    nothing, which is the exact failure this corpus exists to document.

    `agent` names who fired this invocation: "claude-code" for the
    PreToolUse hook (the default, for every existing call site), or
    whatever another agent's own adapter passes through --match --agent.
    It lets --stats tell them apart once more than one shows up in the log.
    """
    try:
        log_path = get_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": kind,
            "subject": subject,
            "matched": matched_titles,
            "count": len(matched_titles),
            "pages": pages_loaded,
            "corpus": str(corpus_dir),
            "agent": agent,
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared matching + logging, used by both hook mode and --match mode
# ---------------------------------------------------------------------------

def _match_and_log(kind, subject, agent):
    """Load the corpus, match `subject` against it by `kind` ("path" or
    "command"), log the invocation, and return the matched pages.

    This is the one place matching + logging happens. Hook mode and --match
    mode both call it so there is exactly one matching path for both: a
    transport-neutral adapter (see DOCS.md) gets the same behavior the
    PreToolUse hook has always had, including the command redaction rule.
    """
    patterns_dir = get_patterns_dir()
    pages, _skipped = load_corpus(patterns_dir)

    if kind == "path":
        matched = match_path_pages(pages, subject)
        log_subject = subject
    else:
        matched = match_command_pages(pages, subject)
        log_subject = command_log_subject(subject)

    titles = [p.title for p in matched]
    log_event(log_subject, kind, titles, len(pages), patterns_dir, agent)
    return matched


def _matched_text(matched):
    """Every matched page's full raw text, joined with a blank line between
    pages -- exactly as additionalContext carries it. None on no match."""
    if not matched:
        return None
    return "\n\n".join(p.content for p in matched)


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

def _emit(matched):
    text = _matched_text(matched)
    if text is None:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(output, separators=(",", ":")))


def run_hook():
    raw = sys.stdin.read()

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(payload, dict):
        return

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return

    tool_name = payload.get("tool_name")

    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return

        matched = _match_and_log("path", file_path, "claude-code")
        _emit(matched)

    elif tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            return

        matched = _match_and_log("command", command, "claude-code")
        _emit(matched)

    # Any other tool_name (or none at all): do nothing. Nothing in this
    # corpus is triggered by any tool other than Edit, Write and Bash, and
    # a hook that does not recognise the invocation should stay silent
    # rather than guess.


# ---------------------------------------------------------------------------
# --match mode
# ---------------------------------------------------------------------------

def run_match():
    """tripwire.py --match --path <path> | --match --command <command>

    Transport-neutral match mode: answers the matching question directly,
    with no hookSpecificOutput envelope, so another agent's own adapter
    (e.g. plugin/opencode/precedent.ts) does not have to reimplement
    matching. Prints matching pages' markdown, joined by a blank line,
    exactly as additionalContext carries it today, or nothing on no match.

    Exactly one of --path or --command is required; neither, or both, is a
    malformed invocation and produces no output and no log entry -- caught
    the same way a missing file_path/command is caught in hook mode.

    --agent <name> is recorded in the log entry as "agent" (default
    "claude-code"), so --stats can tell which agent fired a given line.

    Always exits 0, same as every other mode (see main()): this can be
    invoked from inside another agent's own process rather than as a
    subprocess, so it must never raise or block that process either.
    """
    argv = sys.argv[2:]
    path = None
    command = None
    agent = "claude-code"

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--path" and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
        elif arg == "--command" and i + 1 < len(argv):
            command = argv[i + 1]
            i += 2
        elif arg == "--agent" and i + 1 < len(argv):
            agent = argv[i + 1]
            i += 2
        else:
            # An unrecognised flag, or a flag with no value: malformed.
            return

    # Exactly one of --path / --command is required.
    if (path is None) == (command is None):
        return

    if path is not None:
        if not path:
            return
        matched = _match_and_log("path", path, agent)
    else:
        if not command:
            return
        matched = _match_and_log("command", command, agent)

    text = _matched_text(matched)
    if text is not None:
        sys.stdout.write(text)


# ---------------------------------------------------------------------------
# --check mode
# ---------------------------------------------------------------------------

# The starter page --init writes. It is embedded rather than copied from the
# repository's examples/ directory on purpose: a marketplace install copies
# only plugin/claude-code/, so anything sitting beside it never arrives. The
# same trap that would have made the corpus itself unreachable.
STARTER_PAGE = """# A gate that cannot fail

**Trigger:** you are adding, changing, or relying on CI, a test runner, a
lint or format check, or any automated gate.

**Trigger paths:** `.github/workflows/**` `**/Makefile`

**Class:** the check reports success without having checked.

## What goes wrong

A gate is added, it goes green, and everyone reads green as evidence. But the
gate never ran, or ran against nothing, or its exit status was swallowed on
the way out. The failure is silent by construction: a gate that cannot fail
looks exactly like a gate that passes.

## Evidence

Replace this section with your own, verbatim. Two independent occurrences in
different places, quoted from commits, issues or logs. A claim nobody can
check is not evidence, and a page admitted without it is just an opinion that
fires on every edit.

## Check before you trust a gate

- Make it fail on purpose, once, and watch it go red. A gate never observed
  failing is not yet a gate.
- Check the exit status survives the whole pipeline. A pipe, a `tail`, a
  `|| true` or a trailing command replaces it.
- Count what ran. A runner that discovers work can discover nothing and still
  exit 0; assert the count is non-zero.
"""


def run_init():
    """Create the corpus directory and, if it is empty, a page to start from."""
    patterns_dir = get_patterns_dir()
    print("precedent tripwire init")
    print("  corpus: {}".format(patterns_dir))

    try:
        patterns_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("  could not create it: {}".format(exc))
        print("  set PRECEDENT_PATTERNS to a writable directory and try again.")
        return

    existing = sorted(q.name for q in patterns_dir.glob("*.md") if q.name != "index.md")
    if existing:
        print("  already holds {} page(s): {}".format(len(existing), ", ".join(existing)))
        print("  nothing written. Run --check to see what loads.")
        return

    target = patterns_dir / "a-gate-that-cannot-fail.md"
    try:
        target.write_text(STARTER_PAGE, encoding="utf-8")
    except OSError as exc:
        print("  could not write the starter page: {}".format(exc))
        return

    print("  wrote {}".format(target.name))
    print("")
    print("  That page is a starting point, not a finding: replace its")
    print("  Evidence section with two occurrences of your own before")
    print("  trusting it. Then run --check.")


def run_session_start():
    """Say once, at session start, when there is nothing to surface.

    Installing the plugin and pointing it at a corpus are two steps, and the
    second one is easy to miss. Without this, a fresh install is indis-
    tinguishable from a working one that happens to be quiet: the hook runs on
    every edit, finds no pages, exits 0 and says nothing. That is the exact
    failure this corpus exists to document, so the tool must not ship it.

    Plain stdout from SessionStart DOES reach the model, unlike PreToolUse, so
    this prints nothing at all when a corpus is loaded. A healthy install is
    silent and costs nothing.
    """
    patterns_dir = get_patterns_dir()
    try:
        pages, _skipped = load_corpus(patterns_dir)
    except Exception:
        return
    if pages:
        return

    print("precedent is installed but has no pattern pages, so it will not "
          "surface anything. Looked in {}. Run the tripwire with --init to "
          "create a corpus there with a page to start from, or set "
          "PRECEDENT_PATTERNS to a corpus you already keep.".format(patterns_dir))


def run_version():
    """Print the version, read from the plugin manifest rather than a constant.

    A version hardcoded here is a second place to update, and two declarations
    of the same fact drift. The manifest is the one the plugin host reads, so
    it is the one that is true. If it cannot be read, say so rather than
    printing a number that might be wrong.
    """
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            print("precedent {}".format(json.load(fh)["version"]))
    except Exception:
        print("precedent (version unknown: could not read {})".format(manifest))


def run_check():
    """Say out loud where the corpus is and what loaded, so a broken install
    is visible in one command instead of showing up as silence."""
    patterns_dir = get_patterns_dir()
    if os.environ.get("PRECEDENT_PATTERNS"):
        source = "PRECEDENT_PATTERNS"
    elif os.environ.get("PRECEDENT_HOME"):
        source = "PRECEDENT_HOME"
    else:
        source = "default"
    print("precedent tripwire check")
    print("  script:  {}".format(Path(__file__).resolve()))
    print("  corpus:  {}  (from {})".format(patterns_dir, source))
    print("  exists:  {}".format(patterns_dir.is_dir()))

    if not patterns_dir.is_dir():
        print("")
        print("  NOT WIRED UP. The hook will run, exit 0 and do nothing.")
        print("  Set PRECEDENT_PATTERNS to the corpus directory.")
        return

    pages, skipped = load_corpus(patterns_dir)
    print("  pages loaded: {}".format(len(pages)))
    for page in pages:
        print("    {}".format(page.title))
        print(
            "      trigger paths:   {}".format(
                " ".join(page.path_globs) if page.path_globs else "(none)"
            )
        )
        print(
            "      trigger command: {}".format(
                " ".join(page.command_globs) if page.command_globs else "(none)"
            )
        )
    if skipped:
        print("  skipped:")
        for filename, reason in skipped:
            print("    {}: {}".format(filename, reason))
    if not pages:
        print("")
        print("  NOT WIRED UP. The directory exists but carries no usable pages.")


# ---------------------------------------------------------------------------
# --stats mode
# ---------------------------------------------------------------------------

def _entry_kind(entry):
    kind = entry.get("kind")
    # Log lines written before command triggers existed have no "kind" at
    # all; every one of them came from an Edit/Write path match, so "path"
    # is the correct default, not a guess.
    return kind if kind in ("path", "command") else "path"


def _entry_subject(entry):
    # New lines carry "subject"; older lines carry "path". Read both so
    # existing log lines still parse under the new format.
    subject = entry.get("subject")
    if subject is None:
        subject = entry.get("path", "")
    return subject


def _entry_agent(entry):
    # Log lines written before --match existed carry no "agent" field at
    # all; every one of them came from the claude-code PreToolUse hook, so
    # that is the correct default, not a guess.
    agent = entry.get("agent")
    return agent if isinstance(agent, str) and agent else "claude-code"


def run_stats():
    log_path = get_log_path()

    if not log_path.is_file():
        print("No tripwire log found at {}.".format(log_path))
        return

    total = 0
    hits = 0
    misses = 0
    malformed_lines = 0
    first_ts = None
    last_ts = None
    pattern_freq = {"path": {}, "command": {}}
    hit_subject_freq = {"path": {}, "command": {}}
    miss_subject_freq = {"path": {}, "command": {}}
    kind_totals = {"path": 0, "command": 0}
    agent_totals = {}
    blind = 0
    blind_corpora = {}

    with open(log_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                malformed_lines += 1
                continue

            total += 1
            ts = entry.get("ts")
            if isinstance(ts, str):
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            kind = _entry_kind(entry)
            subject = _entry_subject(entry)
            agent = _entry_agent(entry)
            matched = entry.get("matched") or []
            kind_totals[kind] = kind_totals.get(kind, 0) + 1
            agent_totals[agent] = agent_totals.get(agent, 0) + 1

            # An entry written with no corpus loaded is not a miss. It means
            # the hook ran against nothing, and counting it as a miss would
            # hide a broken install behind a plausible-looking hit rate.
            pages = entry.get("pages")
            if pages == 0:
                blind += 1
                corpus = entry.get("corpus") or "(unknown)"
                blind_corpora[corpus] = blind_corpora.get(corpus, 0) + 1

            if matched:
                hits += 1
                hit_subject_freq[kind][subject] = hit_subject_freq[kind].get(subject, 0) + 1
                for title in matched:
                    pattern_freq[kind][title] = pattern_freq[kind].get(title, 0) + 1
            else:
                misses += 1
                miss_subject_freq[kind][subject] = miss_subject_freq[kind].get(subject, 0) + 1

    def print_freq_table(freq):
        if not freq:
            print("  (none)")
            return
        for key, count in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
            print("  {:4d}  {}".format(count, key))

    def print_freq_table_by_kind(freq_by_kind):
        for kind in ("path", "command"):
            print("  [{}]".format(kind))
            print_freq_table(freq_by_kind[kind])

    if blind:
        print("!! {} of {} invocations ran with NO pattern pages loaded.".format(blind, total))
        print("   The hook fired, exited 0 and did nothing. Those are not misses.")
        for corpus, n in sorted(blind_corpora.items(), key=lambda kv: (-kv[1], kv[0])):
            print("   looked in: {}  ({} times)".format(corpus, n))
        print("   Installing from a marketplace copies only plugin/claude-code/,")
        print("   so patterns/ does not travel with it. Set PRECEDENT_PATTERNS")
        print("   to the corpus directory and re-check with --check.")
        print("")

    print("precedent tripwire stats")
    print("  log: {}".format(log_path))
    print("  invocations: {}".format(total))
    print(
        "  by kind: path={} command={}".format(
            kind_totals.get("path", 0), kind_totals.get("command", 0)
        )
    )
    # Only worth a line once more than one agent shows up in the log; a
    # single-agent install (every existing one, before --match existed)
    # stays exactly as it read before this field existed.
    if len(agent_totals) > 1:
        print(
            "  by agent: {}".format(
                " ".join(
                    "{}={}".format(a, n) for a, n in sorted(agent_totals.items())
                )
            )
        )
    if blind:
        print("  ran without a corpus: {}".format(blind))
    print("  hits: {}".format(hits))
    print("  misses: {}".format(misses))
    print("  hit rate: {}".format("{:.1%}".format(hits / total) if total else "n/a"))
    if first_ts and last_ts:
        print("  span: {} .. {}".format(first_ts, last_ts))
    if malformed_lines:
        print("  malformed log lines skipped: {}".format(malformed_lines))

    print("\nmatched patterns by frequency:")
    print_freq_table_by_kind(pattern_freq)

    print("\nmatched subjects by frequency:")
    print_freq_table_by_kind(hit_subject_freq)

    print("\nsubjects that did not match, by frequency:")
    print_freq_table_by_kind(miss_subject_freq)

    patterns_dir = get_patterns_dir()
    _pages, skipped = load_corpus(patterns_dir)
    print("\ncorpus pages skipped:")
    if skipped:
        for filename, reason in skipped:
            print("  {}: {}".format(filename, reason))
    else:
        print("  (none)")


# ---------------------------------------------------------------------------
# Entry point — exit 0, always
# ---------------------------------------------------------------------------

def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--stats":
            run_stats()
        elif len(sys.argv) > 1 and sys.argv[1] == "--check":
            run_check()
        elif len(sys.argv) > 1 and sys.argv[1] == "--init":
            run_init()
        elif len(sys.argv) > 1 and sys.argv[1] == "--session-start":
            run_session_start()
        elif len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
            run_version()
        elif len(sys.argv) > 1 and sys.argv[1] == "--match":
            run_match()
        else:
            run_hook()
    except Exception:
        # A memory layer that breaks edits or commands is worse than no
        # memory layer.
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
