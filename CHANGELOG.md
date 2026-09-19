# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-09-19

### Added

- **Retirement, as a state of its own.** A page carrying a `**Retired:**`
  line loads no triggers and can never fire again, and `--check` and
  `--stats` report it under *retired on purpose* with its stated reason,
  separately from a page *skipped as broken*. The two used to be one list,
  and printed together a typo reads exactly like a decision. A wrapped
  reason is read to the end of its paragraph rather than truncated at the
  first line, because half a sentence still reads like a whole one.
- **`--match --full`**, which prints the unstripped page for a human
  checking what a page actually says.
- **`injected_chars`** on every telemetry line: what the invocation really
  put in front of the model, which `--stats` totals. Log lines written
  before the field existed are counted apart rather than read as zero.
- **`tools/mine.py --ledger <path>`**, and the append-only markdown format
  it reads: one decision per line, `- <date> | rejected|admitted|retired |
  <subject> | obs: <ids> | <why>`. It skips any candidate group whose
  observation id set was already rejected, and says at the end of the run
  how many it skipped and why — suppressing silently would be the same
  failure in a new place. Keyed on observation ids because group ids are
  assigned by rank within one run and persisted nowhere. A malformed line
  is ignored, counted, and named with its reason. `--ledger` also applies
  to `--draft` (its report goes to stderr, keeping stdout a clean page),
  and every draft now ends with the ledger line for its own ids.

### Changed

- **A matching page no longer injects its evidence.** What reaches the
  model is the page with its `## Evidence` section removed, and only that
  section: what comes after it is injected like the rest. Evidence is what
  a human needed in order to admit the page, not something the model acts
  on while an edit is pending; it stays on disk for the human, for
  `--match --full` and for the miner. Measured on real pages this removes
  24-48% of a match.

  Cutting from the heading to the end of the file was the first rule and it
  was wrong: both pages written so far put their checklist *below* their
  evidence, so it would have injected "what goes wrong" and dropped "what to
  do about it". It would also have imposed a section order on page authors
  without saying so, and an unstated layout constraint is one nobody
  follows.
- `--session-start` no longer tells someone with an all-retired corpus
  that they have no pattern pages and should run `--init`. It surfaces
  nothing either way, but one of those is a missing install and the other
  is a decision.
- `load_corpus()` now returns `(pages, retired, skipped)` and
  `parse_page()` returns a fifth element, the retirement reason.

### Removed

- **The `patterns/index.md` concept.** The corpus needs no index file and
  nothing ever read one: `--check` is the index, derived from the pages
  themselves, so it cannot drift from them the way a hand-maintained list
  does. The filename is still excluded from loading, so a leftover
  `index.md` in an existing corpus is never suddenly parsed as a pattern
  page, and `--check` now reports that it is present and unread.

## [0.5.0] - 2026-09-19

### Added

- **OpenCode support**, through `plugin/opencode/precedent.ts`. It is weaker
  than the Claude Code integration and the docs say so rather than claiming
  parity: OpenCode's `tool.execute.before` can read or mutate a tool's
  arguments and can abort the call by throwing, but it has no field for
  injecting context, so the adapter uses `tool.execute.after` and appends the
  matching page to the tool result. The pattern therefore arrives **after**
  the edit, in time for the agent's next action rather than before the one it
  is about.
- `--match --path` / `--match --command`: a transport-neutral mode that
  answers the matching question and prints the page, so an adapter for
  another agent does not reimplement matching. The `PreToolUse` hook mode now
  calls the same internal function.
- `--agent <name>`, recorded on every log entry, so `--stats` can break
  results down when more than one agent is firing.
- `--version`, read from the plugin manifest rather than a constant, because
  two declarations of the same fact drift.

### Documentation

- An agent support matrix in both `README.md` and `DOCS.md`, stating for each
  agent where it hooks, when the pattern arrives, and what was actually
  checked. Codex's hook file shape matches Claude Code's but no `PreToolUse`
  example or enumerated event list was found, so it is listed as unverified
  rather than guessed either way. Pi has a pre-tool `tool_call` event that can
  block or mutate arguments but not inject context, plus a `tool_result` event
  that can modify what the model reads, so an adapter shaped like the OpenCode
  one is feasible and the docs point at where to start.
- Why no MCP server is offered: it would add tools and always-on tokens, and
  it would turn surfacing into a pull the agent has to remember to perform,
  which is the failure this tool exists to prevent.

## [0.4.0] - 2026-09-19

### Added

- `--session-start`: a new `tripwire.py` mode, wired as a `SessionStart`
  hook (matcher `startup|clear`) in
  `plugin/claude-code/hooks/hooks.json`. Installing the plugin and pointing
  it at a corpus are two separate steps, and a missing second step used to
  produce an install indistinguishable from a working-but-quiet one — the
  `PreToolUse` hook ran on every edit, found no pages, exited 0 and said
  nothing. `--session-start` runs once per session and prints one paragraph,
  naming the corpus directory and how to fix it (`--init` or
  `PRECEDENT_PATTERNS`), only when there is nothing to surface. When a
  corpus with at least one usable page is loaded, it prints nothing at all:
  plain stdout from `SessionStart` reaches the model, unlike `PreToolUse`,
  so a healthy install still costs zero tokens.

## [0.3.0] - 2026-09-19

First release.

### Added

- `plugin/claude-code/scripts/tripwire.py`: a stdlib-only `PreToolUse` hook
  that reads a corpus of markdown pattern pages and surfaces any page whose
  declared triggers match an about-to-run `Edit`, `Write` or `Bash` call, as
  `additionalContext`.
- Two independent trigger matchers: path globs (`**Trigger paths:**`, with
  `*`/`**` semantics bounded against pathological `**` chains) and command
  globs (`**Trigger command:**`, matched against the whole command string).
- `--check`: reports the resolved corpus directory, whether it exists, and
  every page that loaded or was skipped, with a reason.
- `--init`: creates the corpus directory and writes one starter page if the
  directory is empty; idempotent, and safe on an unwritable target.
- `--stats`: reads the telemetry log back and reports hits, misses, a
  breakdown by kind, and a loud warning for any invocation that ran with no
  corpus loaded at all.
- JSONL telemetry at `<PRECEDENT_HOME>/tripwire.jsonl`, one line per
  invocation, with command subjects reduced to their leading words before
  logging (never truncated by character count).
- `tools/mine.py`: an offline, read-only miner over a local engram store
  that groups similar `**Learned**` lessons by cross-project recurrence and
  can emit a draft pattern page skeleton from a candidate group.
- `examples/a-retry-that-never-retries.md`: a worked example of the pattern
  page format.
- Configuration via `PRECEDENT_PATTERNS` and `PRECEDENT_HOME`, resolved
  independently of the script's own install location so a marketplace
  install (which ships only `plugin/claude-code/`) never silently loses the
  corpus.
