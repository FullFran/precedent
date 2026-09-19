# precedent — reference

This is the full reference for the `precedent` tripwire hook and the offline
miner. For the pitch and a quickstart, see [README.md](README.md).

## Contents

- [The pattern page format](#the-pattern-page-format)
- [The two matchers](#the-two-matchers)
- [The hook contract](#the-hook-contract)
- [Configuration](#configuration)
- [CLI modes](#cli-modes)
- [Telemetry](#telemetry)
- [`tools/mine.py`](#toolsminepy)
- [Troubleshooting](#troubleshooting)

## The pattern page format

A pattern page is one markdown file under the corpus directory (see
[Configuration](#configuration)). `patterns/index.md`, if present, is always
excluded from loading — it is reserved for a human-readable table of
contents, not a pattern itself.

A page is parsed by scanning its lines, independently of one another:

| Line | Required | What it does |
|---|---|---|
| `# <title>` | yes | The first line starting with `# ` becomes the page's title. Everything before it is ignored; everything after the first such line does not change the title. |
| `**Trigger paths:**` followed by one or more backtick-delimited globs | no | Matched against `tool_input.file_path` on an `Edit` or `Write` call, with [path glob](#path-globs) semantics. |
| `**Trigger command:**` followed by one or more backtick-delimited globs | no | Matched against `tool_input.command` on a `Bash` call, with [command glob](#command-globs) semantics. |

Everything else in the file — a `**Trigger:**` prose line, `**Class:**`, `##`
sections, evidence, checklists — is free-form markdown. It is not parsed at
all; it is only ever emitted whole, as `additionalContext`, when the page
matches.

**What happens when a required piece is missing:**

- **No `# ` heading anywhere in the file.** The page is skipped entirely
  (title is `None`; nothing is loaded). `--check` and `--stats` report it as
  skipped with the reason `no '# ' heading found`.
- **A heading, but neither a `**Trigger paths:**` nor a `**Trigger command:**`
  line.** The page is skipped: `no '**Trigger paths:**' or '**Trigger
  command:**' line found`. A title alone is not enough to make a page
  loadable, because it could never match anything.
- **A trigger line is present but has no backtick-delimited glob on it**
  (e.g. `**Trigger paths:**` with nothing after it). The page is skipped,
  with a reason naming which line was empty.
- **Only one of the two trigger lines is present.** That is not an error —
  a page may be path-only, command-only, or carry both. It loads normally
  with an empty list for the kind it doesn't declare.

A page that fails to parse is never fatal to the rest of the corpus: it is
recorded in `skipped` (visible via `--check` and `--stats`) and every other
page still loads. A page that can't be read at all (I/O error, or bytes that
aren't valid UTF-8) is skipped the same way, with the underlying exception as
the reason.

## The two matchers

There are two independent matchers, on purpose, because a file path and a
shell command string have different structure.

### Path globs

Matched against `tool_input.file_path` on `Edit`/`Write`.

- `*` matches within a single path segment and never crosses `/`.
- `**` matches zero or more *whole* segments — `**/Makefile` matches a bare
  `Makefile` as well as `sub/dir/Makefile`, because the leading `**` can
  match zero segments.
- Matching is against the whole path, not a substring: `**/Makefile` does
  not match `Makefile2` or `NotAMakefile`.

Two things keep this bounded no matter how the glob is written, because it
runs on every single edit:

1. **Consecutive `**` runs collapse to one `**`** before matching, so
   `**/**/**/**/x` behaves exactly like `**/x`.
2. **Failed `(pattern-index, path-index)` pairs are memoised** during the
   match, turning what would otherwise be exponential backtracking into
   `O(pattern_len × path_len)`. Without that memoisation, *k* runs of `**`
   against a *d*-segment path explores on the order of `C(d+k, k)` branches:
   an earlier implementation took 2.3s at *k*=8, *d*=30, and did not finish
   within 10 seconds at *d*=40. With memoisation, the same shape of pattern
   against a 40-segment path is bounded well under a second (see
   `tests/test_tripwire.py::TestPathologicalGlob`).

#### Worked path glob examples

| Pattern | Path | Result |
|---|---|---|
| `*.spec.ts` | `app.spec.ts` | matches |
| `*.spec.ts` | `src/app.spec.ts` | **no match** — `*` never crosses `/` |
| `**/Makefile` | `Makefile` | matches — leading `**` matches zero segments |
| `**/Makefile` | `sub/dir/Makefile` | matches |
| `**/Makefile` | `Makefile2` | **no match** — whole-segment match, not substring |
| `**/*.spec.ts` | `app.spec.ts` or `src/app.spec.ts` | matches both |
| `.github/workflows/**` | `.github/workflows/ci.yml` | matches |
| `.github/workflows/**` | `.github/workflows` (bare) | matches — trailing `**` also matches zero segments |
| `.github/workflows/**` | `.github/other/ci.yml` | **no match** |
| `**/*_test.go` | `internal/glob/glob_test.go` | matches |
| `**/*_test.go` | `internal/glob/glob.go` | **no match** |

### Command globs

Matched against the whole `tool_input.command` string on `Bash`. This is a
**separate, simpler matcher** — deliberately not sharing code with the path
matcher: a command line has no path segments to reason about, so `*` matches
any run of characters (including spaces and slashes), and there is no `**`
special case at all — it's just two adjacent single-character wildcards.
Each pattern compiles to a cached regex the first time it's used, anchored to
match the whole command string.

#### Worked command glob examples

| Pattern | Command | Result |
|---|---|---|
| `gh * --body*` | ``gh issue comment --body "see `owner/repo#1`"`` | matches |
| `gh * --body*` | `gh pr create --repo owner/repo --body 'text here'` | matches — `*` spans the repo flag and its value |
| `gh * --body-file*` | `gh issue create --body-file /tmp/x.md` | matches |
| `gh * --body*` | `ls -la` | **no match** |
| `gh * --body*` | `gh issue list --state open` | **no match** — no `--body` flag present |
| `ls -la` | `ls -la` | matches (no wildcard, exact) |
| `ls -la` | `ls -la extra` | **no match** — the whole string must match |
| `gh**body` | `gh issue --body` | matches — `**` here is just two ordinary `*` wildcards, not the path matcher's special case |

## The hook contract

`precedent` is registered as a `PreToolUse` hook (see
`plugin/claude-code/hooks/hooks.json`), invoked by absolute path through
`${CLAUDE_PLUGIN_ROOT}` for `Edit`, `Write` and `Bash` tool calls.

**Input:** the hook JSON payload on stdin, e.g.
`{"tool_name": "Edit", "tool_input": {"file_path": "..."}}` or
`{"tool_name": "Bash", "tool_input": {"command": "..."}}`. Malformed JSON, a
non-object payload, a missing `tool_input`, or a missing/empty
`file_path`/`command` are all handled by returning early — never by raising.

**Output, on a match:** a single line of compact JSON on stdout:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"..."}}
```

`additionalContext` is every matching page's full raw text, joined with a
blank line between pages. This exact shape is required, and it is the
*only* shape that reaches the model:

- Plain stdout from a `PreToolUse` hook reaches only a debug log.
- A top-level `systemMessage` field reaches only the user's own transcript.
- Only `hookSpecificOutput.additionalContext`, in this exact nested shape,
  is read into the model's context.

**Output, on a miss:** nothing at all is written to stdout.

**Exit code: always 0.** This is the safety contract the whole script is
built around, stated directly in its module docstring: *"this script must
always exit 0. It is invoked as a `PreToolUse` hook, where an exit code of 2
denies the tool call outright."* Any failure along the way — malformed
payload, unreadable corpus, unwritable log directory, anything — is
swallowed silently rather than blocking or slowing down an edit or a
command. `main()` wraps every mode in a bare `try/except Exception: pass`
followed by an unconditional `sys.exit(0)`; there is no `set -e` equivalent
here, on purpose, because exit 2 from this hook would deny the tool call it
was supposed to be informing.

## Configuration

Two environment variables, resolved independently:

### `PRECEDENT_HOME`

The directory holding a user's corpus (as `<PRECEDENT_HOME>/patterns`) *and*
the telemetry log (`<PRECEDENT_HOME>/tripwire.jsonl`). Defaults to
`~/.precedent` when unset.

### `PRECEDENT_PATTERNS`

Overrides the corpus directory directly, independent of `PRECEDENT_HOME`. It
does **not** move the telemetry log — that always lives under
`PRECEDENT_HOME` (or its `~/.precedent` default), never under
`PRECEDENT_PATTERNS`.

### Resolution order

For the corpus directory:

1. `PRECEDENT_PATTERNS`, if set — used exactly as given.
2. Otherwise `<PRECEDENT_HOME>/patterns`, where `PRECEDENT_HOME` itself falls
   back to `~/.precedent` if unset.

For the telemetry log: always `<PRECEDENT_HOME or ~/.precedent>/tripwire.jsonl`.

The corpus deliberately does not live beside the script. Resolving it
relative to the script's own location would not survive installation: a
Claude Code marketplace install copies only `plugin/claude-code/`, so a
sibling `patterns/` directory never arrives, and the hook would run, find
nothing, exit 0, and do nothing — silently, and looking exactly like an
honest miss. Point `PRECEDENT_PATTERNS` at a corpus kept anywhere, or
symlink `~/.precedent/patterns` at it, then confirm with `--check`.

## CLI modes

All sample output below was produced by actually running the commands shown
(paths normalized to keep the examples portable).

### Default (hook) mode

Reads the hook JSON payload from stdin. No flag.

```
$ echo '{"tool_name":"Edit","tool_input":{"file_path":".github/workflows/ci.yml"}}' \
    | python3 plugin/claude-code/scripts/tripwire.py
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"# A gate that cannot fail\n\n**Trigger:** you are adding, changing, or relying on CI, a test runner, a\nlint or format check, or any automated gate.\n\n**Trigger paths:** `.github/workflows/**` `**/Makefile`\n\n**Class:** the check reports success without having checked.\n\n## What goes wrong\n\nA gate is added, it goes green, and everyone reads green as evidence. But the\ngate never ran, or ran against nothing, or its exit status was swallowed on\nthe way out. The failure is silent by construction: a gate that cannot fail\nlooks exactly like a gate that passes.\n\n## Evidence\n\nReplace this section with your own, verbatim. Two independent occurrences in\ndifferent places, quoted from commits, issues or logs. A claim nobody can\ncheck is not evidence, and a page admitted without it is just an opinion that\nfires on every edit.\n\n## Check before you trust a gate\n\n- Make it fail on purpose, once, and watch it go red. A gate never observed\n  failing is not yet a gate.\n- Check the exit status survives the whole pipeline. A pipe, a `tail`, a\n  `|| true` or a trailing command replaces it.\n- Count what ran. A runner that discovers work can discover nothing and still\n  exit 0; assert the count is non-zero.\n"}}
```

A miss produces nothing on stdout:

```
$ echo '{"tool_name":"Edit","tool_input":{"file_path":"src/unrelated.py"}}' \
    | python3 plugin/claude-code/scripts/tripwire.py
$
```

A command trigger works the same way for `Bash`:

```
$ echo '{"tool_name":"Bash","tool_input":{"command":"gh issue comment --body \"see it\""}}' \
    | python3 plugin/claude-code/scripts/tripwire.py
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"# Backticks execute in a shell body\n\n**Trigger:** you are running gh with a body argument.\n\n**Trigger command:** `gh * --body*` `gh * --body-file*`\n\n**Class:** bash executes backticks in command text instead of passing it through.\n"}}
```

### `--init`

Creates the corpus directory and, if it is empty, writes one starter page.
Idempotent: run twice and the second run reports what's already there and
writes nothing.

```
$ python3 plugin/claude-code/scripts/tripwire.py --init
precedent tripwire init
  corpus: .precedent/patterns
  wrote a-gate-that-cannot-fail.md

  That page is a starting point, not a finding: replace its
  Evidence section with two occurrences of your own before
  trusting it. Then run --check.

$ python3 plugin/claude-code/scripts/tripwire.py --init
precedent tripwire init
  corpus: .precedent/patterns
  already holds 1 page(s): a-gate-that-cannot-fail.md
  nothing written. Run --check to see what loads.
```

The starter page is **embedded in the script**, not copied from this
repository's `examples/` directory — for the same reason the corpus doesn't
live beside the script: a marketplace install only ships
`plugin/claude-code/`, so anything sitting beside it (like `examples/`)
never arrives.

If the target directory can't be created (permissions, a read-only
filesystem, a path component that's actually a file instead of a
directory), `--init` reports the problem in plain text and still exits 0 —
it never raises and never fails the process it's running in:

```
$ PRECEDENT_HOME=blocked/precedent-home python3 plugin/claude-code/scripts/tripwire.py --init
precedent tripwire init
  corpus: blocked/precedent-home/patterns
  could not create it: [Errno 20] Not a directory: 'blocked/precedent-home/patterns'
  set PRECEDENT_PATTERNS to a writable directory and try again.
```

(here `blocked` is a plain file, not a directory, which is what forces
`mkdir(parents=True)` to fail deterministically)

### `--check`

Says out loud where the corpus is, whether it exists, and exactly what
loaded — so a broken install is visible in one command instead of showing up
as silence.

```
$ python3 plugin/claude-code/scripts/tripwire.py --check
precedent tripwire check
  script:  /path/to/precedent/plugin/claude-code/scripts/tripwire.py
  corpus:  .precedent/patterns  (from PRECEDENT_PATTERNS)
  exists:  True
  pages loaded: 1
    Backticks execute in a shell body
      trigger paths:   (none)
      trigger command: gh * --body* gh * --body-file*
```

When the corpus directory doesn't exist at all:

```
$ python3 plugin/claude-code/scripts/tripwire.py --check
precedent tripwire check
  script:  /path/to/precedent/plugin/claude-code/scripts/tripwire.py
  corpus:  .precedent/patterns  (from script location)
  exists:  False

  NOT WIRED UP. The hook will run, exit 0 and do nothing.
  Set PRECEDENT_PATTERNS to the corpus directory.
```

Note the `(from ...)` label only distinguishes `PRECEDENT_PATTERNS` from
everything else — when it reads `(from script location)`, the corpus is
actually resolved from `PRECEDENT_HOME`/`~/.precedent`, not from the
script's own directory; nothing is ever read relative to the script (see
[Configuration](#configuration)).

### `--stats`

Reads the telemetry log back and reports hits, misses, and a breakdown by
kind (`path` vs `command`).

```
$ python3 plugin/claude-code/scripts/tripwire.py --stats
precedent tripwire stats
  log: .precedent/tripwire.jsonl
  invocations: 2
  by kind: path=2 command=0
  hits: 1
  misses: 1
  hit rate: 50.0%
  span: 2026-09-19T14:14:15Z .. 2026-09-19T14:14:15Z

matched patterns by frequency:
  [path]
     1  A gate that cannot fail
  [command]
  (none)

matched subjects by frequency:
  [path]
     1  .github/workflows/ci.yml
  [command]
  (none)

subjects that did not match, by frequency:
  [path]
     1  src/unrelated.py
  [command]
  (none)

corpus pages skipped:
  (none)
```

If invocations ran with **no corpus loaded at all** (`pages == 0` in the log
entry), that is called out first, loudly, and is never folded into the miss
count — a run against nothing and a run that genuinely found no match are
different failures that call for different responses:

```
!! 1 of 1 invocations ran with NO pattern pages loaded.
   The hook fired, exited 0 and did nothing. Those are not misses.
   looked in: .precedent/patterns  (1 times)
   Installing from a marketplace copies only plugin/claude-code/,
   so patterns/ does not travel with it. Set PRECEDENT_PATTERNS
   to the corpus directory and re-check with --check.

precedent tripwire stats
  log: .precedent/tripwire.jsonl
  invocations: 1
  by kind: path=1 command=0
  ran without a corpus: 1
  hits: 0
  misses: 1
  hit rate: 0.0%
  ...
```

If the log doesn't exist yet, `--stats` says so in one sentence and stops:

```
$ python3 plugin/claude-code/scripts/tripwire.py --stats
No tripwire log found at .precedent/tripwire.jsonl.
```

## Telemetry

Every invocation of hook mode — hit or miss, with or without a corpus —
appends exactly one JSON line to `<PRECEDENT_HOME>/tripwire.jsonl`. Logging
is best-effort: any exception while writing (unwritable directory, whatever)
is swallowed, and the hook's own behavior (its stdout, its exit code) is
never affected by whether the log write succeeded.

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | UTC timestamp, `YYYY-MM-DDTHH:MM:SSZ`. |
| `kind` | `"path"` or `"command"` | Which matcher ran: `path` for `Edit`/`Write`, `command` for `Bash`. |
| `subject` | string | What was checked. For `kind: "path"`, the file path, written **in full, unredacted**. For `kind: "command"`, the *redacted* command (see below). |
| `matched` | array of string | Titles of every page that matched. Empty on a miss. |
| `count` | integer | `len(matched)`. |
| `pages` | integer | How many pattern pages were loaded for this invocation. `0` means the hook ran against an empty or missing corpus — see the blind-run warning under [`--stats`](#--stats). |
| `corpus` | string | The resolved corpus directory path for this invocation. |

Older log lines, written before command triggers existed, carry `path`
instead of `subject` and have no `kind` field at all. `--stats` reads both:
a missing `kind` is treated as `"path"` (every log line from before command
triggers existed came from a path match), and `subject` falls back to the
legacy `path` field when absent. `tripwire.jsonl` is meant to accumulate
across format changes without breaking `--stats`.

### The redaction rule

A file path is logged in full — paths don't carry secrets the way a
`Bash` command line routinely does. A command is different: it can carry
credentials, tokens or other data a file path never would, so before it's
ever written to disk it is reduced to its leading words by
`command_log_subject()`.

**Truncating to a character count is not redaction**, and this is the
reason: truncation protects only by accident of position.

- `gh issue comment --body "..." && curl -H "Authorization: Bearer sk-..."` —
  the `Authorization` header sits inside the first 80 characters of that
  command, so a length-limit truncation would write it out in full.
- `TOKEN=sk-abc gh issue comment` — the secret is the *first* word, so a
  length limit gets it exactly backwards: every prefix of the command
  contains it.

Instead, only the leading words survive: tokens are kept in order, and the
scan stops as soon as it hits a token that starts with `-` (a flag) or
contains `=` (an environment assignment) — which covers both shapes above.
Up to `COMMAND_LOG_WORDS` (currently 3) leading words are kept; if none
survive, the subject is logged as the literal string `"(redacted)"`.

```
"gh issue comment --body 'short'"                          -> "gh issue comment"
"gh auth login --with-token < /dev/stdin"                   -> "gh auth login"
"AWS_SECRET_ACCESS_KEY=abc aws s3 cp a b"                    -> "(redacted)"
"TOKEN=sk-LEAKED gh issue comment --body 'x'"                -> "(redacted)"
```

What survives is enough to answer the question the log exists for — is a
command glob too narrow, or too wide — and nothing more.

## `tools/mine.py`

`patterns/*.md` pages are written by hand. `tools/mine.py` is an *offline*,
read-only miner over a third-party memory tool's local SQLite store (default
`~/.engram/engram.db`), meant to surface raw material for more pages, not to
write them.

**What it does:**

1. Opens the database read-only, through a `file:...?mode=ro` URI plus
   `PRAGMA query_only = ON` (belt-and-suspenders — the URI mode already
   forbids writes).
2. Verifies the columns it depends on actually exist, rather than trusting a
   schema description that might be stale.
3. Selects live (`deleted_at IS NULL`) rows of a fixed set of types
   (`bugfix`, `discovery`, `decision`, `architecture`, `pattern` —
   `session_summary`, task-state and checkpoint rows are excluded: they're
   long, narrative, and written for a different reader, and would swamp
   everything else by sheer volume) whose content contains a `**Learned**`
   section, and extracts just that section.
4. Normalizes each lesson's text (strips fenced/inline code, URLs, path-like
   tokens, camelCase identifiers, `snake_case` tokens, filenames, long hex
   strings; lowercases; drops a small hand-picked English+Spanish stopword
   list) into a set of content words.
5. Groups lessons whose token sets are similar (Jaccard similarity above a
   threshold) using single-link agglomeration via union-find.
6. Ranks groups by **how many distinct projects** they span, not by group
   size — a lesson repeated across several commits in one project is
   ordinary iteration; the same lesson reappearing in a *different* project
   is the re-derivation this tool exists to surface.
7. Prints a report, or, with `--draft <group id>`, a skeleton pattern page
   pre-filled with that group's verbatim evidence.

**What it cannot do:** it does not identify a failure class. Its own report
says so explicitly, every time, because lexical similarity is not the same
thing as two lessons being the same class — "the CI never ran," "fmt-check
could not fail," and "the pipeline reported tail's exit status" are one
class through abstraction only a human supplies, not through shared
vocabulary. A high-ranked group can be unrelated lessons that happen to use
similar words; a real cross-project class can be split across several groups
because two people described it differently. Naming the class, and deciding
whether a group actually is one, stays a human decision — `mine.py`
proposes, a human disposes.

```
$ python3 tools/mine.py --db path/to/engram.db --min-projects 2
1 candidate group(s) met the thresholds; showing 1

group 1: 2 distinct project(s), 2 observation(s)
  projects: repo-a, repo-b
  date span: 2026-06-01 to 2026-07-14
  members:
    - [repo-a] 2026-06-01 obs #1 "Fixed CI gate": the ci gate reported success without the test suite ever running
    - [repo-b] 2026-07-14 obs #2 "Fixed release gate": the release gate reported success without the test suite ever running
  terms in common: without, reported, ever, gate, success, suite, running, test

---
Read this before treating anything above as a finding: this is lexical
grouping, not class identification. The "terms in common" line is the only
thing the tool actually knows two lessons share -- it does not know they are
the same failure class, and it cannot tell a real recurrence from a lexical
coincidence. "The CI never ran", "fmt-check could not fail" and "the
pipeline reported tail's exit status" are one class -- a gate that cannot
fail -- through abstraction that only a human supplies, not through any
words they have in common. A high-ranked group here may be several unrelated
lessons that happen to use similar words, and a real cross-project class may
be split across several groups because two people described it differently.
Naming the class, and deciding whether a group is one, is the human's job.
Nothing above is a pattern page; it is raw material for one.
```

```
$ python3 tools/mine.py --db path/to/engram.db --min-projects 2 --draft 1
# TODO: name the class

**Trigger:** TODO -- describe when a human should open this page

**Trigger paths:**

**Class:** TODO -- one line, what goes wrong

## What goes wrong

TODO

## Evidence

candidate group 1 -- 2 project(s), 2 observation(s), 2026-06-01 to 2026-07-14
(mined, not reviewed):

`repo-a`, observation #1, 2026-06-01 -- "Fixed CI gate":

    the ci gate reported success without the test suite ever running

`repo-b`, observation #2, 2026-07-14 -- "Fixed release gate":

    the release gate reported success without the test suite ever running

## Check before you trust this

- TODO
```

Every field in a draft's Evidence section is a verbatim quote from the
store, never a paraphrase — the same admission rule the corpus applies to a
hand-written page (see [CONTRIBUTING.md](CONTRIBUTING.md)) applies here too:
a claim nobody can check is not evidence. `--help` lists every flag,
including `--limit`, `--similarity` and `--db`; group ids are only stable
within one invocation that shares `--db`, `--min-projects` and
`--similarity` — they are not persisted anywhere.

## Troubleshooting

### It does not seem to fire

Run `--check` first. It answers the two questions that matter, in order:
does the corpus directory exist at all, and if so, did anything actually
load from it.

```
python3 plugin/claude-code/scripts/tripwire.py --check
```

- `exists: False` — `PRECEDENT_PATTERNS`/`PRECEDENT_HOME` points nowhere, or
  is unset and nothing was ever created at the default `~/.precedent`. Run
  `--init`, or point one of those variables at a corpus you already have.
- `exists: True` but `pages loaded: 0` — the directory exists but every
  file in it either isn't `.md`, is `index.md`, or failed to parse. Check
  the `skipped:` list `--check` prints underneath for the reason.
- Pages loaded, but a specific edit or command still doesn't trigger one —
  compare the glob against your path/command using the tables in
  [The two matchers](#the-two-matchers); a `**` that doesn't cross `/`, or a
  command flag hidden behind an unrelated `*`, is the usual cause.

If it was firing before and now silently isn't, check `--stats` for a run of
`pages: 0` entries (the blind-run warning) — that means the environment the
hook actually runs in (which may not be your interactive shell) has lost
`PRECEDENT_PATTERNS`, most often after a marketplace reinstall, since that
only ships `plugin/claude-code/` and never the corpus.

### Everything else

`--check` and `--stats` are read-only and safe to run at any time — neither
ever writes to the corpus, and `--init` never overwrites one that already
has pages in it. When in doubt, run both.
