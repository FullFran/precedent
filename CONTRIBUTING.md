# Contributing

## Running the tests

```bash
python3 -m unittest discover tests
```

Add `-v` for per-test output. There are two suites: `tests/test_tripwire.py`
covers the hook (both matchers, page parsing, the hook contract, telemetry,
redaction, `--check`/`--init`/`--stats`), and `tests/test_mine.py` covers the
offline miner. Both run against real subprocess invocations and temp
directories — nothing touches your actual `~/.precedent` or `~/.engram`.

Also worth running before sending anything:

```bash
python3 -m py_compile plugin/claude-code/scripts/tripwire.py tools/mine.py
```

## Adding a pattern page

A page earns its place in the corpus by having been true more than once.
The admission rule is:

- **Two independent occurrences.** The same failure class, seen in at least
  two different places (two commits, two repos, two sessions — not two
  lines of the same commit).
- **Verbatim evidence.** Quote the actual commit message, log line, or issue
  text, with its source. A paraphrase is not evidence — the point of quoting
  it verbatim is that someone else can check it against the source.
- **No evidence, no page.** A pattern you believe but can't point at twice is
  a hunch, not a precedent page, no matter how confident it feels while
  you're staring at the bug. Write it down somewhere else until it recurs.

`tools/mine.py --draft <group id>` will get you the skeleton with the
evidence section already filled in verbatim from the store it read — but the
class name, the `## What goes wrong` explanation, the trigger globs, and the
`## Check before you trust this` section are still yours to write. Mining
proposes; you dispose.

Write what the model should read *above* the page's `## Evidence` heading.
Everything from that heading down stays on disk and is never injected (see
[DOCS.md](DOCS.md#what-is-injected-the-head-not-the-page)) — the evidence is
there so a human can check the page, not so the model can re-read it on
every match.

When you dispose, record it. The draft ends with the ledger line for its own
observation ids; paste it into your ledger with the decision and the reason,
and `mine.py --ledger <path>` will stop proposing a group you already
rejected — and will say so out loud when it does. The format is in
[DOCS.md](DOCS.md#the-decision-ledger). A page that stops being true does not
have to be deleted either: a `**Retired:**` line keeps the evidence and stops
the page firing.

## The one rule for code changes

**Never make a path that can exit non-zero or block a tool call.**

`plugin/claude-code/scripts/tripwire.py` is a `PreToolUse` hook. An exit code
of 2 from a `PreToolUse` hook denies the tool call it was invoked for — so
any change to this script that lets an exception escape `main()`, or that
adds a new exit path outside the existing `try/except Exception: pass` guard
followed by `sys.exit(0)`, is a bug, not a feature, however reasonable the
failure looks in isolation. A memory layer that breaks edits or commands is
worse than no memory layer. If you're adding a new failure mode, add a test
in `TestSafetyMatrix` alongside it that proves the process still exits 0.
