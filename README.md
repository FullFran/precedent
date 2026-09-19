# precedent

`precedent` is a corpus of markdown pages, plus a `PreToolUse` hook that reads
one of them into the model's context before an `Edit`, `Write` or `Bash` call
that matches it. It exists so a failure class you already wrote down gets
read *before* the work that would repeat it, not after.

## The problem

A failure class that recurs costs real time. A measurement across five
repositories found that between 7% and 29% of genuine defect commits
re-derived a lesson already learned somewhere else, and two classes recurred
across both repository and language boundaries.

Those repositories were not short of written lessons: fifteen architecture
decision records existed across two of them, and **every one was written in
the same commit or session as the fix it documented**. They were retrospective.
Nothing read a lesson before the work that would repeat it.

So the gap is not writing lessons down. It is that:

1. nothing reads one before the work, and
2. they are per-repository, while the recurrence is cross-repository.

## What it does

`patterns/*.md` is a corpus you write — short pages, one failure class each,
with real evidence. A `PreToolUse` hook matches the file path or the command
of an about-to-run `Edit`, `Write` or `Bash` call against each page's
declared triggers, and puts every matching page's full text into the model's
context as `additionalContext`.

It is a hook, not an MCP server: Claude Code reports it as
`harness-only — no model context cost`, and it costs ~0 tokens and runs in
about 28ms. No database, no server, no build step — one stdlib-only Python
script.

## Install

1. In Claude Code, add this repository as a plugin marketplace source, then
   install the `precedent` plugin from it:
   ```
   /plugin marketplace add FullFran/precedent
   /plugin install precedent@precedent
   ```
   A local checkout works too, which is what you want while writing pages:
   `/plugin marketplace add /path/to/this/repo`. Either way, if the plugin
   does not show as active immediately, run `/reload-plugins`.

   The plugin adds no tools and no always-on tokens. It is a hook, not an MCP
   server, and Claude Code reports it as `harness-only — no model context
   cost`.
2. Point it at a corpus. The default is `~/.precedent/patterns`, so a symlink
   is enough if you already keep your pages in a repository of your own:
   ```
   ln -s /path/to/your/corpus ~/.precedent/patterns
   ```
   Otherwise set `PRECEDENT_PATTERNS`, or just run `--init` below and use the
   default. You don't have to remember to check this yourself: a fresh
   install announces itself once, at the start of your next session, if it
   still has no corpus to read from. A working install stays silent.

The corpus deliberately does **not** ship inside the plugin: a marketplace
install copies only `plugin/claude-code/`, and your patterns are your own
writing about your own work, not something a plugin install should own. See
[DOCS.md](DOCS.md#configuration) for the full resolution order.

### OpenCode

`plugin/opencode/precedent.ts` is a second, weaker adapter for
[OpenCode](https://opencode.ai) — see [Agent support](#agent-support) below
for exactly how it's weaker before you rely on it.

1. Copy or symlink it into `~/.config/opencode/plugins/`:
   ```
   ln -s /path/to/this/repo/plugin/opencode/precedent.ts \
     ~/.config/opencode/plugins/precedent.ts
   ```
2. It needs `plugin/claude-code/scripts/tripwire.py` reachable — by default
   it resolves the script relative to its own file (one directory up, into
   `../claude-code/scripts/`), which is already true inside this repo. If
   you copy the `.ts` file somewhere that path doesn't hold, set
   `PRECEDENT_TRIPWIRE` to the script's absolute path.
3. Verify it: make an edit that matches one of your corpus pages'
   trigger paths (or run a matching `Bash` command), and check the pattern
   was appended to the end of that tool call's result, delimited by a
   `---` line naming `plugin/opencode/precedent.ts`.

## 60-second quickstart

```bash
# Create the corpus directory and a starter page to replace.
python3 plugin/claude-code/scripts/tripwire.py --init

# Confirm it's wired up: where the corpus lives, what loaded.
python3 plugin/claude-code/scripts/tripwire.py --check
```

`--init` writes one starter page, `a-gate-that-cannot-fail.md`, and is
idempotent — it never overwrites an existing corpus. Replace its Evidence
section with two verbatim occurrences of your own before trusting it (see
[CONTRIBUTING.md](CONTRIBUTING.md) for the admission rule), then edit
anything under `.github/workflows/` or touch a `Makefile` in Claude Code and
watch the page surface before the edit lands.

## The pattern page format

A page is markdown with a heading and at least one trigger line:

```markdown
# A gate that cannot fail

**Trigger:** you are adding, changing, or relying on CI, a test runner, a
lint or format check, or any automated gate.

**Trigger paths:** `.github/workflows/**` `**/Makefile`

**Trigger command:** `make *` `* --retries *`

**Class:** the check reports success without having checked.

## What goes wrong
...

## Evidence
...

## Check before you trust it
...
```

- **Trigger paths** — backtick-delimited globs matched against the file path
  of an `Edit` or `Write` call.
- **Trigger command** — backtick-delimited globs matched against the whole
  command string of a `Bash` call.

A page may carry either line, both, or neither. Carrying neither means it is
loaded but never matches anything, which `--check` reports as skipped. Full
grammar, matcher semantics and worked examples are in
[DOCS.md](DOCS.md#the-pattern-page-format).

See [`examples/a-retry-that-never-retries.md`](examples/a-retry-that-never-retries.md)
for a complete worked page (its own Evidence section is explicitly marked as
invented, since it's the format example, not a real finding).

## What this is not

- **Not a memory system.** It does not store, summarize or generalize
  anything about a session. It only surfaces markdown pages a human wrote.
- **Not a linter.** It never inspects the content of an edit or a diff, only
  the path and the command about to run.
- **Not a search index.** Matching is deterministic glob matching against
  declared triggers, not ranking, embeddings or fuzzy retrieval.

## Agent support

All matching logic lives in one place —
[`tripwire.py --match`](DOCS.md#--match) — so an adapter for another agent
only has to shell out and place the result. Where the result lands, and when
it arrives relative to the tool call it's about, differs per agent and is
not cosmetic:

| agent | hook point | when the pattern arrives | status |
|---|---|---|---|
| Claude Code | `PreToolUse` → `additionalContext` | before the edit | supported |
| OpenCode | `tool.execute.after` → appended to the result | after the edit | supported, weaker |
| Codex | hooks.json shape matches Claude Code's, but `PreToolUse` support is **unverified** | — | not implemented |
| Pi | has a pre-tool `tool_call` event, but it can only block or mutate arguments, not inject context — see [DOCS.md](DOCS.md#agent-support) | — | not implemented |
| any MCP agent | — | — | out of scope: this is a hook, not an MCP server, on purpose |

See [DOCS.md](DOCS.md#agent-support) for what "weaker" costs on OpenCode,
exactly what was checked for Codex and Pi, and why an MCP server is
deliberately not offered.

## More

- [DOCS.md](DOCS.md) — the full reference: page format, both matchers, the
  hook contract, configuration, every CLI mode with real output, telemetry,
  and troubleshooting.
- [CONTRIBUTING.md](CONTRIBUTING.md) — running the tests and the admission
  rule for a new pattern page.
- [CHANGELOG.md](CHANGELOG.md)
