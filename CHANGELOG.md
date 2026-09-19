# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
