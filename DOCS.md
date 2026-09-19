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
- [Agent support](#agent-support)
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

`precedent` registers two hooks (see `plugin/claude-code/hooks/hooks.json`),
both invoked by absolute path through `${CLAUDE_PLUGIN_ROOT}`: a
`PreToolUse` hook for `Edit`, `Write` and `Bash` tool calls (default/hook
mode, no flag), and a `SessionStart` hook, matcher `startup|clear`, that runs
`tripwire.py --session-start` (see [`--session-start`](#--session-start)).

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
*only* shape that reaches the model **from a `PreToolUse` hook**:

- Plain stdout from a `PreToolUse` hook reaches only a debug log.
- A top-level `systemMessage` field reaches only the user's own transcript.
- Only `hookSpecificOutput.additionalContext`, in this exact nested shape,
  is read into the model's context.

**Output, on a miss:** nothing at all is written to stdout.

That rule is specific to `PreToolUse`, and does not generalize. A handful of
other hook events — `UserPromptSubmit`, `UserPromptExpansion`,
`SessionStart`, and `PostModelSwitch` — read **plain stdout** into the
model's context directly, with no `hookSpecificOutput` wrapper needed. That
is exactly why `--session-start` prints nothing on a healthy install: for
`PreToolUse`, silence on stdout is the null case regardless — nothing there
was ever going to be read. For `SessionStart`, silence is a deliberate
choice — anything printed is read on every single session, so a chatty hook
would spend context for no reason, and printing only costs something when
there is a genuine problem to report.

**Exit code: always 0.** This is the safety contract the whole script is
built around, stated directly in its module docstring: *"this script must
always exit 0. It is invoked as a `PreToolUse` hook, where an exit code of 2
denies the tool call outright."* Any failure along the way — malformed
payload, unreadable corpus, unwritable log directory, anything — is
swallowed silently rather than blocking or slowing down an edit or a
command. `main()` wraps every mode in a bare `try/except Exception: pass`
followed by an unconditional `sys.exit(0)`; there is no `set -e` equivalent
here, on purpose, because exit 2 from this hook would deny the tool call it
was supposed to be informing. The same wrapper covers `--session-start`, so
a `SessionStart` invocation exits 0 no matter what it finds.

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

### `--match`

```
tripwire.py --match --path <path>
tripwire.py --match --command <command>
```

A transport-neutral match mode: it answers the matching question directly,
with no `hookSpecificOutput` envelope, so an adapter belonging to an agent
other than Claude Code (see [Agent support](#agent-support)) doesn't have to
reimplement matching, only shell out and place the result. It shares the
same internal matching function hook mode uses — there is exactly one
matching path in this script, not two that could drift apart.

Exactly one of `--path` or `--command` is required. An optional `--agent
<name>` (default `claude-code`) is recorded in the log entry as `agent`, so
[`--stats`](#--stats) can tell which agent fired a given line.

Prints every matching page's markdown, joined by a blank line — exactly what
`additionalContext` carries in hook mode, just without the JSON wrapper —
or nothing at all on no match:

```
$ python3 plugin/claude-code/scripts/tripwire.py --match --path Makefile
# A gate that cannot fail

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

$ python3 plugin/claude-code/scripts/tripwire.py --match --path src/unrelated.py
$
```

A command match, with `--agent` set the way `plugin/opencode/precedent.ts`
sets it:

```
$ python3 plugin/claude-code/scripts/tripwire.py --match \
    --command 'gh issue comment --body "see it"' --agent opencode
# Backticks execute in a shell body

**Trigger:** you are running gh with a body argument.

**Trigger command:** `gh * --body*` `gh * --body-file*`

**Class:** bash executes backticks in command text instead of passing it through.
```

That last invocation's log line carries the agent through, and the command
subject is still redacted to its leading words the same way hook mode
redacts it:

```
{"ts": "2026-09-19T17:05:35Z", "kind": "command", "subject": "gh issue comment", "matched": ["Backticks execute in a shell body"], "count": 1, "pages": 2, "corpus": ".precedent/patterns", "agent": "opencode"}
```

Neither `--path` nor `--command`, or both at once, is a malformed
invocation: it prints nothing, logs nothing, and — like every other mode —
still exits 0.

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

### `--session-start`

Registered as a `SessionStart` hook (matcher `startup|clear`, see
`plugin/claude-code/hooks/hooks.json`), so it runs once when a session starts
or is cleared — not on every edit. Its job is to make a corpus-less install
visible without anyone having to remember to run `--check`: installing the
plugin and pointing it at a corpus are two separate steps, and a missing
second step otherwise produces an install indistinguishable from a
working-but-quiet one. When the corpus has at least one usable page, it
prints **nothing at all**:

```
$ PRECEDENT_HOME=.precedent python3 plugin/claude-code/scripts/tripwire.py --session-start
$
```

It prints nothing on purpose, not because there is nothing to say. Unlike
`PreToolUse`, plain stdout from a `SessionStart` hook reaches the model (see
[The hook contract](#the-hook-contract)) — a chatty hook here would spend
context on every single session for no reason, so a healthy install has to
stay silent. When the corpus is missing or holds no usable pages, it prints
one paragraph naming the directory it looked in and exactly how to fix it:

```
$ PRECEDENT_HOME=.precedent python3 plugin/claude-code/scripts/tripwire.py --session-start
precedent is installed but has no pattern pages, so it will not surface anything. Looked in .precedent/patterns. Run the tripwire with --init to create a corpus there with a page to start from, or set PRECEDENT_PATTERNS to a corpus you already keep.
```

An unusable corpus (every page fails to parse) is treated the same as a
missing one — both mean the hook has nothing to surface, so both get the
notice. Like every other mode, it always exits 0: an unreadable corpus
directory is swallowed the same way `load_corpus()` swallows it everywhere
else, and never raises.

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

`by agent:` only appears once more than one agent shows up in the log — a
single-agent install (every install that predates
[`--match`](#--match), and every install that only ever uses the Claude
Code hook) reads exactly as it always has. Once a second agent's adapter
(e.g. `plugin/opencode/precedent.ts`, which logs as `opencode`) has fired at
least once, the line appears:

```
$ python3 plugin/claude-code/scripts/tripwire.py --stats
precedent tripwire stats
  log: .precedent/tripwire.jsonl
  invocations: 3
  by kind: path=2 command=1
  by agent: claude-code=2 opencode=1
  hits: 2
  misses: 1
  hit rate: 66.7%
  span: 2026-09-19T17:05:35Z .. 2026-09-19T17:05:35Z
  ...
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
| `agent` | string | Who fired this invocation: `"claude-code"` for the `PreToolUse` hook, or whatever another agent's adapter passed to [`--match --agent`](#--match) (`"opencode"` for `plugin/opencode/precedent.ts`). |

Older log lines, written before command triggers existed, carry `path`
instead of `subject` and have no `kind` field at all. `--stats` reads both:
a missing `kind` is treated as `"path"` (every log line from before command
triggers existed came from a path match), and `subject` falls back to the
legacy `path` field when absent. Older log lines also predate `agent`
entirely — `--stats` treats a missing `agent` as `"claude-code"`, since
every one of them came from the hook before `--match` existed. `tripwire.jsonl`
is meant to accumulate across format changes without breaking `--stats`.

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

## Agent support

All matching lives in one place: [`tripwire.py --match`](#--match). An
adapter for another agent's hook surface shells out to it and places the
result somewhere the model will read it — it never reimplements page
parsing or glob matching itself. What differs per agent is *where* that
placement is possible at all, and that difference is not cosmetic.

| agent | hook point | when the pattern arrives | status |
|---|---|---|---|
| Claude Code | `PreToolUse` → `additionalContext` | before the edit | supported |
| OpenCode | `tool.execute.after` → appended to the result | after the edit | supported, weaker |
| Codex | hooks.json shape matches Claude Code's, but `PreToolUse` support is **unverified** | — | not implemented |
| Pi | has a pre-tool `tool_call` event, but it can only block or mutate arguments, not inject context | — | not implemented |
| any MCP agent | — | — | out of scope, on purpose |

### Claude Code — supported

The baseline this whole tool is built around: see
[The hook contract](#the-hook-contract). `additionalContext` from a
`PreToolUse` hook is read into the model's context *before* the `Edit`,
`Write` or `Bash` call it was invoked for runs. The model sees the page and
can still decide not to make the mistake in the first place.

### OpenCode — supported, weaker

`plugin/opencode/precedent.ts` uses OpenCode's `tool.execute.after` hook,
and only that one. This was not a stylistic choice — it's the only hook
OpenCode exposes that can put text in front of the model at all:

- `"tool.execute.before"(input: {tool, sessionID, callID}, output: {args})`
  → the only mutable thing is `output.args`, the tool's own arguments.
  There is no field here to inject context for the model to read, and
  throwing aborts the tool call outright rather than informing it. This is
  the hook that would need to exist for OpenCode to get Claude Code's
  before-the-edit behavior, and it doesn't have the shape for it.
- `"tool.execute.after"(input: {tool, sessionID, callID, args}, output:
  {title, output, metadata})` → `output.output` is the tool result string
  the model actually reads, and it's mutable. This is where
  `plugin/opencode/precedent.ts` appends the matched page, clearly
  delimited so it's never mistaken for the tool's own output.

The consequence: on OpenCode, the pattern page arrives *after* the edit (or
the command) has already run, attached to that same tool call's result. The
model reads it before its *next* action, so it can still course-correct —
but the edit that would have been prevented on Claude Code has already
landed by the time the model sees the page. That's a real difference in
what the tool can do for you, not a detail. Don't treat OpenCode's adapter
as equivalent to Claude Code's; treat it as a weaker net.

Tool names: OpenCode's built-ins are registered under lowercase ids —
`edit`, `write`, `bash` — not Claude Code's capitalized `Edit`/`Write`/
`Bash`. This was verified two ways, not guessed by analogy: against the
installed `@opencode-ai/plugin` type definitions (`Hooks["tool.execute.after"]`'s
`input.tool: string`, and the existing OpenCode plugins already installed
locally that canonicalize tool names with `.toLowerCase()` before comparing
them), and directly against the compiled `opencode` binary itself, which
contains the literal registrations `ID="bash"` / `H.register({[ID]: ...})`,
`rP="edit"` / `H.register({[rP]: ...})`, and `DN="write"` /
`H.register({[DN]: ...})`.

### Codex — not implemented, unverified

What was checked: a real, locally installed Codex CLI, its `codex plugin`
subcommand, and the two actual third-party Codex plugins with a
`hooks.json` present in a local plugin cache. Both use the exact same
`{"hooks": {"<Event>": [{"matcher": "...", "hooks": [{"type": "command",
"command": "..."}]}]}}` shape Claude Code's `hooks.json` uses, with the
same tool matcher strings (`"Write|Edit"`, `"Bash"`) — but both only
registered `PostToolUse` and `Stop` handlers. Neither used `PreToolUse`.
Codex's own bundled plugin authoring reference documents the plugin
manifest's `hooks` field only as "Hook config path" — it points at
`hooks.json` without enumerating which event names Codex actually
recognizes or dispatches.

So "unverified" means exactly that: the file *shape* Codex's plugin system
expects is confirmed identical to Claude Code's, but nothing locally
available confirms whether a `PreToolUse` entry in that file is recognized,
ignored, or handled differently (e.g. without delivering pre-tool context to
the model the way Claude Code's `additionalContext` does). What's missing to
finish this: a working `PreToolUse` example (upstream or your own), run
against a real `codex` session, that proves whether the equivalent of
`additionalContext` reaches the model before the tool call, or at all. Until
that's confirmed, don't assume a `PreToolUse` entry in a Codex
`hooks.json` behaves like this tool's Claude Code integration.

### Pi — not implemented

What was checked: Pi's installed coding-agent package's own bundled docs
(`docs/extensions.md`) and its extension API type definitions
(`dist/core/extensions/types.d.ts`). Pi's extension system is not a
`hooks.json` file the way Claude Code's and (apparently) Codex's are — it's
a TypeScript extension API, loaded via `--extension` or auto-discovery, with
its own event names.

That API *does* have a pre-tool event: `tool_call` fires "after
`tool_execution_start`, before the tool executes," is documented as
"**Can block**," and its handler can mutate `event.input` in place (typed
per built-in tool, including `bash`/`edit`/`write` — the same lowercase
names OpenCode uses). But its return type, `ToolCallEventResult`, is
`{ block?: boolean; reason?: string }` and nothing else — there is no field
to inject arbitrary context into what the model sees before the tool runs.
That's the same limitation OpenCode's `tool.execute.before` has, for the
same reason: a pre-tool hook here can gate or reshape the call, not narrate
to the model about it.

Pi also has a post-tool event, `tool_result`, whose handler can return
`{ content, details, isError }` to modify what the model reads after the
tool executes — structurally the same capability OpenCode's
`tool.execute.after` has, which is what `plugin/opencode/precedent.ts` is
built on. So a Pi adapter in the same "supported, weaker" shape as the
OpenCode one looks feasible by the same mechanism, in principle — it just
wasn't built as part of this change, since only the OpenCode adapter was in
scope. A reader who wants to finish this can model it on
`plugin/opencode/precedent.ts`: shell out to
`tripwire.py --match --agent pi`, and append the result from a
`pi.on("tool_result", ...)` handler using `isToolCallEventType`/
`isBashToolResult` to read the matched tool's path or command.

### Any MCP agent — out of scope, on purpose

An MCP server is not offered, deliberately, for two reasons:

1. **It would add tools and always-on tokens.** Every MCP tool this server
   exposed would sit in every agent's tool list on every turn, whether or
   not a pattern ever matched — the opposite of the "harness-only, ~0
   tokens" cost this tool is built around (see
   [What it does](README.md#what-it-does)).
2. **It would turn surfacing into a *pull* the agent has to remember to
   perform.** An MCP tool only fires when the model decides to call it.
   That's exactly the failure class this tool exists to prevent: a lesson
   that exists somewhere but isn't read before the work that would repeat
   it (see [The problem](README.md#the-problem)). A hook is a *push* — it
   runs whether or not the model thought to ask — and that's the entire
   reason this is a hook and not an MCP server.

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
