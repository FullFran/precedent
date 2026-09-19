#!/usr/bin/env python3
"""precedent tripwire — PreToolUse hook surfacing failure-class pattern pages.

Reads the corpus in patterns/*.md directly from disk and matches it against
the tool about to run:

  - Edit / Write: trigger-path globs against tool_input.file_path.
  - Bash:         trigger-command globs against tool_input.command.

A page may carry a `**Trigger paths:**` line, a `**Trigger command:**`
line, both, or neither (in which case it is reported as broken). A page
carrying a `**Retired:**` line loads no triggers and can never fire again,
and is reported as retired rather than as broken — a decision and a typo
must not read the same way. Emits additionalContext for every matching
page. No database, no server, no build step: one stdlib-only script
invoked by absolute path through ${CLAUDE_PLUGIN_ROOT}.

What is injected is the page's HEAD: everything above its `## Evidence`
heading. The evidence itself stays on disk, where a human and the miner
can still read it, instead of being paid for on every single match. A page
with no `## Evidence` heading is injected whole. `--match --full` prints
the unstripped page, for a human checking what a page actually says.

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
                            (default "claude-code"), recorded in the log,
                            and --full to print the unstripped page.
    tripwire.py --stats     reads the telemetry log back and prints a report.
    tripwire.py --check     reports what corpus is loaded, each loaded
                            page's trigger globs, each retired page's
                            stated reason, and each broken page's fault.
                            This output IS the corpus index; there is no
                            index file, and nothing reads one.
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
RETIRED_LINE_PREFIX = "**Retired:**"

# A `**Retired:**` line with nothing after it is still a retirement — the
# line is the decision — but it has nothing to report, and saying so is
# better than printing an empty reason that reads like a missing one.
NO_REASON_GIVEN = "(no reason given)"

# The corpus needs no index file. This name is excluded from loading for
# one reason only: an existing corpus may still contain one left over from
# when this tool reserved the name, and it must not suddenly start being
# parsed as a pattern page. Nothing reads it; --check says so when it is
# there, and --check itself is the index (derived from the pages, so it
# cannot drift from them the way a hand-maintained list does).
INDEX_FILENAME = "index.md"

# The heading that separates what the model is asked to act on from the
# justification a human needed in order to admit the page. Matched on the
# heading text, not the exact line, so `### Evidence`, `## Evidence (two
# occurrences)` and a lowercased spelling all split the same way.
EVIDENCE_HEADING_RE = re.compile(r"^#{2,6}[ \t]+Evidence\b", re.IGNORECASE)

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
    __slots__ = ("title", "path_globs", "command_globs", "content", "head", "filename")

    def __init__(self, title, path_globs, command_globs, content, head, filename):
        self.title = title
        self.path_globs = path_globs
        self.command_globs = command_globs
        self.content = content
        self.head = head
        self.filename = filename


def split_evidence(text):
    """Split a page into (injected, evidence) by removing its Evidence section.

    Only the `## Evidence` section is removed: from that heading down to the
    next heading of the same or a higher level, or the end of the page. What
    comes after it -- typically the checklist, which is the part the model
    actually acts on -- stays in.

    Cutting from the heading to the end of the file instead was the obvious
    reading and it is wrong. Both pages written so far put their checklist
    BELOW their evidence, so that rule would have injected "what goes wrong"
    and dropped "what to do about it", which is backwards. It would also have
    quietly imposed a section order on anyone writing a page, and a layout
    constraint nobody states is a layout constraint nobody follows.

    Evidence is what a human needed in order to admit the page: two verbatim
    occurrences, with sources. The model never acts on it. It stays on disk
    for the human and the miner, who do read it.

    A page with no Evidence section comes back byte-for-byte unchanged.
    """
    lines = text.splitlines(keepends=True)
    start = None
    level = 0
    for i, line in enumerate(lines):
        match = EVIDENCE_HEADING_RE.match(line)
        if match:
            start = i
            level = len(line) - len(line.lstrip("#").lstrip()) - 1
            level = line.count("#", 0, len(line) - len(line.lstrip("#")))
            break
    if start is None:
        return text, ""

    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if 0 < depth <= level:
                end = j
                break

    kept = "".join(lines[:start]).rstrip()
    tail = "".join(lines[end:]).strip()
    if tail:
        kept = (kept + "\n\n" + tail) if kept else tail
    return (kept + "\n" if kept else ""), "".join(lines[start:end])


def retirement_reason(lines, idx):
    """The reason on the `**Retired:**` line at lines[idx], unwrapped.

    A reason is prose, and prose in this corpus wraps: every `**Trigger:**`
    line in the shipped pages runs over two lines. Reading only the first
    one would report half a sentence and look like a whole one, which is
    worse than reporting nothing -- nobody checks a reason that already
    reads like a reason. Continuation stops where a markdown paragraph
    stops: a blank line, a new `**Marker:**`, or a heading.
    """
    parts = [lines[idx][len(RETIRED_LINE_PREFIX):].strip()]
    for line in lines[idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("**") or stripped.startswith("#"):
            break
        parts.append(stripped)
    return " ".join(p for p in parts if p).strip() or NO_REASON_GIVEN


def parse_page(text):
    """Parse one pattern page's markdown text.

    Returns (title, path_globs, command_globs, skip_reason, retired_reason).

    At most one of the last two is ever set. skip_reason is None when a
    title was found and at least one of the two trigger lines contributed
    at least one backtick-delimited glob; otherwise the fields that were
    found are still returned and skip_reason explains what was missing.
    retired_reason is set, and skip_reason is None, when the page carries
    a `**Retired:**` line.

    The two trigger lines are independent: a page may carry either, both,
    or neither. Carrying neither, with no `**Retired:**` line, is what
    makes a page broken.
    """
    title = None
    path_globs = []
    command_globs = []
    path_line_seen = False
    command_line_seen = False
    retired_reason = None

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if title is None and line.startswith(HEADING_PREFIX):
            title = line[len(HEADING_PREFIX):].strip()
        if line.startswith(TRIGGER_PATH_LINE_PREFIX):
            path_line_seen = True
            path_globs = re.findall(r"`([^`]+)`", line)
        if line.startswith(TRIGGER_COMMAND_LINE_PREFIX):
            command_line_seen = True
            command_globs = re.findall(r"`([^`]+)`", line)
        if retired_reason is None and line.startswith(RETIRED_LINE_PREFIX):
            retired_reason = retirement_reason(lines, idx)

    if title is None:
        # No heading is a broken file, not a retirement, even if it also
        # carries a `**Retired:**` line: there is nothing to report the
        # retirement of, and a file this malformed is more likely a
        # mistake than a decision.
        return None, [], [], "no '# ' heading found", None

    if retired_reason is not None:
        # Retirement beats every trigger line on the page. A retired page
        # loads no globs at all, so it cannot fire again, and the globs it
        # used to carry are deliberately dropped rather than kept around
        # where a later reader might think they are still live.
        return title, [], [], None, retired_reason

    if not path_line_seen and not command_line_seen:
        return (
            title,
            [],
            [],
            "no '**Trigger paths:**', '**Trigger command:**' or "
            "'**Retired:**' line found",
            None,
        )

    if path_globs or command_globs:
        return title, path_globs, command_globs, None, None

    # At least one trigger line was seen, but neither yielded a usable glob.
    if path_line_seen and not command_line_seen:
        reason = "'**Trigger paths:**' line has no backtick-delimited globs"
    elif command_line_seen and not path_line_seen:
        reason = "'**Trigger command:**' line has no backtick-delimited globs"
    else:
        reason = "trigger lines present but neither has a backtick-delimited glob"
    return title, [], [], reason, None


def load_corpus(patterns_dir):
    """Load every usable pattern page under patterns_dir.

    Returns (pages, retired, skipped), three distinct states that must
    stay distinct: conflating them is how a typo hides as a decision.

      pages    loaded, and able to fire: Page objects.
      retired  a decision: (filename, title, reason) for each page whose
               `**Retired:**` line took it out of service. It loads no
               triggers and never fires, and it is not broken.
      skipped  broken: (filename, reason) for each page that could not be
               read or parsed, with what was missing.

    Never raises: a missing or unreadable directory yields empty results.
    """
    pages = []
    retired = []
    skipped = []

    try:
        entries = sorted(patterns_dir.glob("*.md"))
    except OSError:
        return pages, retired, skipped

    for entry in entries:
        # Not a reserved slot for a table of contents, and not a feature:
        # see INDEX_FILENAME. Excluded only so a leftover index.md is
        # never parsed as a pattern page. --check reports it as unread.
        if entry.name == INDEX_FILENAME:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append((entry.name, "unreadable: {}".format(exc)))
            continue

        title, path_globs, command_globs, reason, retired_reason = parse_page(text)
        if retired_reason is not None:
            retired.append((entry.name, title, retired_reason))
            continue
        if reason is not None:
            skipped.append((entry.name, reason))
            continue

        head, _evidence = split_evidence(text)
        pages.append(
            Page(
                title=title,
                path_globs=path_globs,
                command_globs=command_globs,
                content=text,
                head=head,
                filename=entry.name,
            )
        )

    return pages, retired, skipped


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


def log_event(subject, kind, matched_titles, pages_loaded, corpus_dir,
              agent="claude-code", injected_chars=0):
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

    `injected_chars` is how many characters this invocation actually put
    in front of the model -- the stripped length, after split_evidence(),
    not the size of the pages on disk. Without it the log records that a
    page matched but not what the match cost, which is the one number
    that says whether the corpus is worth what it spends. 0 on a miss.
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
            "injected_chars": injected_chars,
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared matching + logging, used by both hook mode and --match mode
# ---------------------------------------------------------------------------

def _match_and_log(kind, subject, agent, full=False):
    """Load the corpus, match `subject` against it by `kind` ("path" or
    "command"), log the invocation, and return the text to emit (None on
    no match).

    This is the one place matching + logging happens. Hook mode and --match
    mode both call it so there is exactly one matching path for both: a
    transport-neutral adapter (see DOCS.md) gets the same behavior the
    PreToolUse hook has always had, including the command redaction rule.

    `full` is the human's escape hatch (--match --full): it emits the
    unstripped pages instead of their heads. The logged injected_chars is
    the length of whatever this invocation actually emitted, so the log
    keeps saying what was really spent rather than what usually is.
    """
    patterns_dir = get_patterns_dir()
    pages, _retired, _skipped = load_corpus(patterns_dir)

    if kind == "path":
        matched = match_path_pages(pages, subject)
        log_subject = subject
    else:
        matched = match_command_pages(pages, subject)
        log_subject = command_log_subject(subject)

    text = _matched_text(matched, full=full)
    titles = [p.title for p in matched]
    log_event(
        log_subject,
        kind,
        titles,
        len(pages),
        patterns_dir,
        agent,
        injected_chars=len(text) if text else 0,
    )
    return text


def _matched_text(matched, full=False):
    """Every matched page's injectable text, joined with a blank line
    between pages -- exactly as additionalContext carries it. None on no
    match.

    That text is each page's HEAD (everything above `## Evidence`), not
    its full contents: see split_evidence(). `full=True` asks for the
    unstripped pages instead, which only --match --full does, for a human
    reading a page rather than a model acting on one.
    """
    if not matched:
        return None
    return "\n\n".join((p.content if full else p.head) for p in matched)


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

def _emit(text):
    if not text:
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

        _emit(_match_and_log("path", file_path, "claude-code"))

    elif tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            return

        _emit(_match_and_log("command", command, "claude-code"))

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

    --full prints the unstripped page instead of the head an agent gets.
    What an agent is given is deliberately not the whole page (see
    split_evidence), and a human checking what a page actually says needs
    a way to read the part that stayed on disk without opening the file
    by hand and working out which one matched.

    Always exits 0, same as every other mode (see main()): this can be
    invoked from inside another agent's own process rather than as a
    subprocess, so it must never raise or block that process either.
    """
    argv = sys.argv[2:]
    path = None
    command = None
    agent = "claude-code"
    full = False

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
        elif arg == "--full":
            full = True
            i += 1
        else:
            # An unrecognised flag, or a flag with no value: malformed.
            return

    # Exactly one of --path / --command is required.
    if (path is None) == (command is None):
        return

    if path is not None:
        if not path:
            return
        text = _match_and_log("path", path, agent, full=full)
    else:
        if not command:
            return
        text = _match_and_log("command", command, agent, full=full)

    if text:
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

    existing = sorted(
        q.name for q in patterns_dir.glob("*.md") if q.name != INDEX_FILENAME
    )
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
        pages, retired, _skipped = load_corpus(patterns_dir)
    except Exception:
        return
    if pages:
        return

    # An all-retired corpus surfaces nothing either, but it is a decision,
    # not a missing install, and telling someone to run --init over it
    # would be telling them to fix something that is not broken.
    if retired:
        print("precedent has {} page(s) in {}, and every one of them is "
              "retired, so it will not surface anything. That is a state, "
              "not a fault. Run the tripwire with --check to see each one "
              "and why it was retired.".format(len(retired), patterns_dir))
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
    is visible in one command instead of showing up as silence.

    This output is also the corpus index. It lists every page and its
    triggers, derived from the pages themselves, so it cannot drift from
    them -- which a second, hand-maintained copy in an index file always
    eventually does. There is no index file, and nothing reads one.

    The three states it reports are kept apart on purpose: a page that
    loaded, a page retired on purpose with its stated reason, and a page
    skipped as broken with what was missing. Printed as one list they
    would read the same, and a typo would hide as a decision.
    """
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

    pages, retired, skipped = load_corpus(patterns_dir)
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

    # Three states, three wordings. A retired page is a decision someone
    # made and can defend; a broken page is a mistake nobody noticed.
    if retired:
        print("  retired on purpose: {}".format(len(retired)))
        for filename, title, reason in retired:
            print("    {}  ({})".format(title, filename))
            print("      reason: {}".format(reason))
            print("      loads no triggers and can never fire. Still on disk.")
    if skipped:
        print("  skipped as broken: {}".format(len(skipped)))
        for filename, reason in skipped:
            print("    {}: {}".format(filename, reason))

    # The corpus needs no index file. Say so where someone will see it,
    # rather than excluding the file silently and letting it look read.
    try:
        has_index = (patterns_dir / INDEX_FILENAME).is_file()
    except OSError:
        has_index = False
    if has_index:
        print("  {}: present, and nothing reads it.".format(INDEX_FILENAME))
        print("      It is not loaded and is not a pattern page. The corpus")
        print("      needs no index file: this --check output is the index,")
        print("      and it is derived from the pages themselves, so it")
        print("      cannot drift from them. Delete it, or keep it as notes")
        print("      for yourself, but nothing will ever read it.")

    if not pages:
        print("")
        if retired and not skipped:
            print("  NOTHING WILL SURFACE. The directory exists and every page")
            print("  in it is retired on purpose. That is a state, not a fault.")
        else:
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
    injected_total = 0
    injected_invocations = 0
    injected_unknown = 0

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

            # Log lines written before the head/evidence split existed
            # carry no injected_chars at all, and every one of them
            # injected a whole page rather than a head. There is no honest
            # number to substitute for them, so they are counted apart
            # instead of being folded in at 0 (which would understate what
            # the corpus used to cost) or at their page size (which is not
            # recorded anywhere in the log).
            injected = entry.get("injected_chars")
            if isinstance(injected, int) and not isinstance(injected, bool):
                if injected > 0:
                    injected_total += injected
                    injected_invocations += 1
            else:
                injected_unknown += 1

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
    if injected_invocations:
        print(
            "  injected: {} chars over {} invocation(s) (mean {})".format(
                injected_total,
                injected_invocations,
                int(round(injected_total / injected_invocations)),
            )
        )
    if injected_unknown:
        print(
            "  invocations predating injected_chars (not counted above): {}".format(
                injected_unknown
            )
        )
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
    _pages, retired, skipped = load_corpus(patterns_dir)
    print("\ncorpus pages skipped as broken:")
    if skipped:
        for filename, reason in skipped:
            print("  {}: {}".format(filename, reason))
    else:
        print("  (none)")

    print("\ncorpus pages retired on purpose:")
    if retired:
        for filename, title, reason in retired:
            print("  {} ({}): {}".format(title, filename, reason))
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
