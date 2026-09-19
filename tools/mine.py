#!/usr/bin/env python3
"""Offline miner over the engram memory store.

`precedent`'s pattern pages (patterns/*.md) are written by hand today. The
raw material for more of them already sits in a third-party tool's local
SQLite store: engram, at ~/.engram/engram.db, keeps every "**Learned**"
lesson anyone has ever had it save, across every project it has touched.
Nobody reads that back before starting new work, which is the whole
for the two probes that show *why* (the lessons are lexically un-findable
and nobody thinks to search for a class before it is one).

This script reads that store read-only, pulls out the **Learned** section of
each qualifying observation, groups lessons that use similar words, and
prints candidate groups ranked by how many distinct projects they span. It
proposes; a human disposes -- see the closing note this script always
prints, and the admission rule in CONTRIBUTING.md for why that gate exists.

Usage:
    python3 tools/mine.py                          # report, defaults
    python3 tools/mine.py --limit 10 --min-projects 2
    python3 tools/mine.py --draft 1                # skeleton page for group 1
    python3 tools/mine.py --ledger decisions.md    # skip what was rejected

Group ids are only stable across invocations that use the same --db,
--min-projects and --similarity: they are assigned by rank within one run,
not persisted anywhere. That is exactly why the ledger keys on observation
ids and never on a group id -- see LEDGER_FORMAT below.
"""

import argparse
import itertools
import re
import sqlite3
import sys
from collections import Counter, defaultdict, namedtuple
from pathlib import Path

DEFAULT_DB = "~/.engram/engram.db"

# The types this tool considers. session_summary, task-state and checkpoint
# are excluded on purpose: they are long, narrative, written for a different
# reader, and would swamp everything else by sheer volume and vocabulary.
LEARNED_TYPES = ("bugfix", "discovery", "decision", "architecture", "pattern")

REQUIRED_COLUMNS = {"id", "type", "title", "content", "project", "created_at", "deleted_at"}

Lesson = namedtuple(
    "Lesson", ["obs_id", "project", "created_at", "title", "text", "tokens"]
)


# ---------------------------------------------------------------------------
# The ledger
#
# An append-only markdown file recording what a human decided about a
# candidate group, one decision per line. It exists because this tool
# proposes the same groups on every run, and a group looked at once and
# rejected costs the same attention the second time it is proposed.
#
# The point of the format is that something READS it. A ledger nothing
# consults is a file that claims to matter and does not, which is the
# failure this corpus documents; so --ledger parses it, suppresses what was
# already rejected, and says how many groups it suppressed and why. It never
# writes to it -- a decision is a human's to record.
#
# The key is the set of OBSERVATION IDS in a group, never a group id. Group
# ids here are assigned by rank within one run (see assemble_groups) and are
# persisted nowhere, so "group 3" means a different group tomorrow, while
# observation ids come from the store and do not move.
# ---------------------------------------------------------------------------

LEDGER_FORMAT = """\
- 2026-09-19 | rejected | miner group | obs: 6570,7523 | shared one generic word; unrelated documents
- 2026-09-19 | admitted | a-gate-that-cannot-fail | obs: 101,102 | two occurrences, two repos
- 2026-09-20 | retired  | some-page | obs: - | stopped matching anything real\
"""

LEDGER_DECISIONS = ("rejected", "admitted", "retired")

LEDGER_ENTRY_RE = re.compile(r"^\s*-\s+(\S.*)$")
LEDGER_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LEDGER_OBS_RE = re.compile(r"^obs\s*:\s*(.*)$", re.IGNORECASE)

NO_REASON_GIVEN = "(no reason given)"

LedgerEntry = namedtuple(
    "LedgerEntry", ["lineno", "date", "decision", "subject", "obs_ids", "note"]
)


def parse_ledger_line(line, lineno=0):
    """Parse one ledger line.

    Returns (entry, error):

      (None, None)      this line is not an entry at all -- a blank line, a
                        heading, ordinary prose. The ledger is markdown and
                        is allowed to have all three; they are not faults.
      (None, "reason")  this line looks like an entry and is malformed. It
                        is ignored, counted, and reported with its reason:
                        a typo that silently changed nothing is exactly the
                        failure a ledger is supposed to prevent.
      (entry, None)     parsed.
    """
    m = LEDGER_ENTRY_RE.match(line)
    if not m:
        return None, None

    parts = m.group(1).split("|", 4)
    if len(parts) < 5:
        return None, "expected 5 '|'-separated fields, found {}".format(len(parts))

    date, decision, subject, obs_field, note = (p.strip() for p in parts)

    if not LEDGER_DATE_RE.match(date):
        return None, "'{}' is not a YYYY-MM-DD date".format(date)

    decision = decision.lower()
    if decision not in LEDGER_DECISIONS:
        return None, "unknown decision '{}' (expected one of: {})".format(
            decision, ", ".join(LEDGER_DECISIONS)
        )

    obs_match = LEDGER_OBS_RE.match(obs_field)
    if not obs_match:
        return None, "fourth field is '{}', expected 'obs: <ids>'".format(obs_field)

    raw_ids = obs_match.group(1).strip()
    obs_ids = set()
    if raw_ids and raw_ids != "-":
        for token in raw_ids.split(","):
            token = token.strip()
            if not token.isdigit():
                return None, "'{}' is not an observation id".format(token)
            obs_ids.add(int(token))

    return (
        LedgerEntry(
            lineno=lineno,
            date=date,
            decision=decision,
            subject=subject,
            obs_ids=frozenset(obs_ids),
            note=note or NO_REASON_GIVEN,
        ),
        None,
    )


def parse_ledger(text):
    """Parse a whole ledger. Returns (entries, malformed).

    malformed is a list of (lineno, line, reason). One bad line never
    aborts a run and never invalidates the lines around it -- an
    append-only file accumulates, and a ledger that refused to be read
    because of one typo would be a ledger nobody keeps.
    """
    entries = []
    malformed = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        entry, error = parse_ledger_line(line, lineno)
        if entry is not None:
            entries.append(entry)
        elif error is not None:
            malformed.append((lineno, line.strip(), error))
    return entries, malformed


def load_ledger(path):
    """Read and parse the ledger at `path`. Returns (entries, malformed)."""
    return parse_ledger(Path(path).read_text(encoding="utf-8"))


def rejected_obs_sets(entries):
    """Map each rejected entry's observation-id set to that entry.

    Only `rejected` decisions suppress anything: `admitted` and `retired`
    record what happened to a page, not that the group should stop being
    proposed. An entry with no ids (`obs: -`) keys nothing -- it can never
    equal a real group's member set, and pretending otherwise would let an
    id-less line suppress something by accident.
    """
    rejected = {}
    for entry in entries:
        if entry.decision != "rejected" or not entry.obs_ids:
            continue
        rejected.setdefault(entry.obs_ids, entry)
    return rejected


# ---------------------------------------------------------------------------
# Stopwords
#
# The corpus is genuinely mixed Spanish and English (the same person writing
# to the same store in whichever language they were thinking in). This list
# is small and hand-picked, not a linguistic resource -- a real stopword list
# per language would mean pulling in NLP tooling, which the brief rules out.
# ---------------------------------------------------------------------------
STOPWORDS = frozenset(
    """
    the a an of to in on for with is was were be been being that this it its
    and or but as at by from into than then so not no nor if when while
    because about after before between during through over under again
    further once here there all any both each few more most other some such
    only own same very can will just should now do does did has have had
    are which who whom what these those would could also one two using used
    use still already instead rather either yet you he she we they them
    their his her our your my me us there's isn't wasn't
    el la los las un una unos unas de del al en por para con sin sobre entre
    hasta desde como que cuando donde porque pero si no es son fue era ser
    estar esta estan está están lo le les su sus mi mis tu tus nos se muy mas
    más menos tambien también ya aun aún aunque pues entonces asi así esto
    eso este ese esa estos esas uno otra otro cada todo toda todos todas hay
    habia había ademas además sino solo sólo cual cuales sea eran fueron
    """.split()
)


# ---------------------------------------------------------------------------
# Extraction: pull the **Learned** section out of an observation's content.
# ---------------------------------------------------------------------------

# A section marker in the mem_save convention: **Word or phrase**: -- the
# colon is what makes it a marker and not just bold text used for emphasis
# somewhere in the middle of a lesson (both occur in the real store).
MARKER_RE = re.compile(r"\*\*[^\n*]{1,80}\*\*\s*:")

LEARNED_MARKER = "**Learned**"


def extract_learned(content):
    """Return the text of the **Learned** section, or None if there isn't one.

    Runs from the literal marker to the next **Something**: marker, or to
    the end of the string. Handles both '**Learned**: text' (the common
    form) and the rarer bare '**Learned**\\ntext' with no colon.
    """
    idx = content.find(LEARNED_MARKER)
    if idx == -1:
        return None
    start = idx + len(LEARNED_MARKER)
    lead = re.match(r"\s*:?\s*", content[start:])
    start += lead.end()
    nxt = MARKER_RE.search(content, start)
    end = nxt.start() if nxt else len(content)
    lesson = content[start:end].strip()
    return lesson or None


# ---------------------------------------------------------------------------
# Normalisation: reduce a lesson to a set of comparable content words.
#
# Order matters here. Code, paths and identifiers are stripped *before*
# lowercasing, because case (a slash, a dot before an extension, a
# lower-to-upper transition) is exactly the signal that tells them apart
# from ordinary prose. Once that's gone, everything is lowercase, and
# punctuation and stopwords are dropped last.
# ---------------------------------------------------------------------------

FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
URL_RE = re.compile(r"https?://\S+")
PATH_TOKEN_RE = re.compile(r"\S*[/\\]\S+")
CAMEL_RE = re.compile(r"\b[a-zA-Z]*[a-z][A-Z][a-zA-Z]*\b")
UNDERSCORE_TOKEN_RE = re.compile(r"\S*_\S+")
FILENAME_RE = re.compile(r"\b[\w-]+\.[A-Za-z]{1,5}\b")
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{7,}\b")
NON_WORD_RE = re.compile(r"[^a-z0-9áéíóúñü\s]")


def normalize(text):
    """Lowercase, strip code/paths/identifiers/punctuation, drop stopwords."""
    text = FENCED_CODE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = PATH_TOKEN_RE.sub(" ", text)
    text = CAMEL_RE.sub(" ", text)
    text = UNDERSCORE_TOKEN_RE.sub(" ", text)
    text = FILENAME_RE.sub(" ", text)
    text = LONG_HEX_RE.sub(" ", text)
    text = text.lower()
    text = NON_WORD_RE.sub(" ", text)
    return [t for t in text.split() if len(t) > 2 and t not in STOPWORDS]


# ---------------------------------------------------------------------------
# Reading the store
# ---------------------------------------------------------------------------

def open_readonly(db_path):
    """Open the engram store through a read-only URI. Cannot write, ever."""
    uri = "file:{}?mode=ro".format(db_path)
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")  # belt-and-suspenders; mode=ro already forbids writes
    return con


def verify_schema(con):
    """Confirm the columns this tool relies on actually exist, rather than
    trusting a description written when the schema was last read."""
    cols = {row[1] for row in con.execute("PRAGMA table_info(observations)")}
    missing = REQUIRED_COLUMNS - cols
    if missing:
        raise SystemExit(
            "observations table is missing expected column(s): {}".format(
                ", ".join(sorted(missing))
            )
        )


def read_lessons(con):
    """Select live rows of the right types whose content has a **Learned**
    section, and extract that section from each."""
    placeholders = ",".join("?" for _ in LEARNED_TYPES)
    query = """
        SELECT id, project, created_at, title, content
        FROM observations
        WHERE deleted_at IS NULL
          AND type IN ({})
          AND content LIKE '%**Learned%'
        ORDER BY id
    """.format(placeholders)
    lessons = []
    for obs_id, project, created_at, title, content in con.execute(query, LEARNED_TYPES):
        lesson_text = extract_learned(content or "")
        if not lesson_text:
            continue
        tokens = frozenset(normalize(lesson_text))
        lessons.append(
            Lesson(
                obs_id,
                project or "(unknown project)",
                created_at or "",
                title or "",
                lesson_text,
                tokens,
            )
        )
    return lessons


# ---------------------------------------------------------------------------
# Grouping: Jaccard similarity over token sets, simple single-link
# agglomeration via union-find. Stdlib only, as instructed.
# ---------------------------------------------------------------------------

def build_groups(lessons, threshold):
    """Union lessons whose token-set Jaccard similarity is >= threshold.

    Returns a dict of union-find root -> list of lesson indices.
    """
    n = len(lessons)
    parent = list(range(n))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    index = defaultdict(list)
    for i, lesson in enumerate(lessons):
        for tok in lesson.tokens:
            index[tok].append(i)

    # A token shared by hundreds of lessons is exactly the kind of word that
    # is *not* discriminative (it is common, not similar), and comparing
    # every pair that shares it is where a naive O(n^2) scan spends most of
    # its time for the least signal. Capping how many lessons a single
    # token's posting list can contribute keeps this off the table without
    # adding a dependency or changing the result for any pair that actually
    # matters: two lessons that are genuinely alike share several tokens,
    # not just one very common one.
    cap = max(40, int(n**0.5) * 6)

    candidates = set()
    for idxs in index.values():
        if len(idxs) < 2 or len(idxs) > cap:
            continue
        candidates.update(itertools.combinations(idxs, 2))

    for a, b in candidates:
        sa, sb = lessons[a].tokens, lessons[b].tokens
        if not sa or not sb:
            continue
        inter = sa & sb
        if not inter:
            continue
        sim = len(inter) / len(sa | sb)
        if sim >= threshold:
            union(a, b)

    members = defaultdict(list)
    for i in range(n):
        members[find(i)].append(i)
    return members


def group_stats(lessons, member_idxs):
    members = [lessons[i] for i in member_idxs]
    projects = sorted({m.project for m in members})
    dates = sorted(m.created_at for m in members if m.created_at)
    date_span = (dates[0][:10], dates[-1][:10]) if dates else ("?", "?")

    term_counts = Counter()
    for m in members:
        term_counts.update(m.tokens)

    # "Terms in common" here means shared by more than one member, not
    # necessarily every member: single-link agglomeration can chain A-B and
    # B-C together without A and C sharing anything directly, so a term
    # literally present in *all* members is not guaranteed to exist even
    # though the group crossed the similarity threshold pairwise. Reporting
    # only per-member-unique terms would be dishonest the other way, so this
    # reports what is actually shared: terms two or more members use.
    min_share = 2 if len(members) > 1 else 1
    common_terms = [t for t, c in term_counts.most_common() if c >= min_share][:12]

    return {
        "members": members,
        "projects": projects,
        "date_span": date_span,
        "common_terms": common_terms,
    }


def group_obs_ids(group):
    """The set of observation ids a group is made of.

    This, and not the group id, is what identifies a group across runs:
    group ids are assigned by rank inside one invocation and persisted
    nowhere, so they mean nothing tomorrow.
    """
    return frozenset(m.obs_id for m in group["members"])


def assemble_groups(lessons, threshold, min_projects, rejected=None):
    """Returns (groups, suppressed).

    `rejected` is the mapping rejected_obs_sets() builds. A candidate group
    whose member id set EQUALS one already rejected is dropped, and comes
    back in `suppressed` as (obs_ids, ledger_entry) so the caller can say
    what it dropped and why. Equality, not overlap: a group that gained or
    lost a member is a different group and has not been ruled on.

    Suppression happens before ranking, so the group ids a run prints are
    contiguous over the groups it actually shows.
    """
    member_map = build_groups(lessons, threshold)
    groups = [group_stats(lessons, idxs) for idxs in member_map.values()]
    groups = [g for g in groups if len(g["projects"]) >= min_projects]

    suppressed = []
    if rejected:
        kept = []
        for g in groups:
            obs_ids = group_obs_ids(g)
            entry = rejected.get(obs_ids)
            if entry is not None:
                suppressed.append((obs_ids, entry))
                continue
            kept.append(g)
        groups = kept

    # ------------------------------------------------------------------
    # Rank by the number of DISTINCT PROJECTS a group spans, descending,
    # then by size. This ordering is the whole point of the tool and comes
    # straight from the measurement this tool exists because of: a lesson repeated
    # inside ONE project is ordinary iteration -- several commits
    # converging on one bug in one place -- and no written pattern would
    # have prevented it. The same lesson reappearing in a DIFFERENT project
    # is re-derivation, which is the one thing a standing cross-project
    # record can actually move. Sorting by size first would instead reward
    # whichever single project happened to produce the most similarly-worded
    # lessons, which is the opposite of what this tool is for.
    # ------------------------------------------------------------------
    groups.sort(key=lambda g: (-len(g["projects"]), -len(g["members"])))

    for i, g in enumerate(groups, start=1):
        g["group_id"] = i
    return groups, suppressed


def find_group(groups, group_id):
    for g in groups:
        if g["group_id"] == group_id:
            return g
    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

HONESTY_NOTE = """\
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
"""


def format_ledger_report(ledger_path, entries, suppressed, malformed):
    """What the ledger did to this run, said out loud.

    Printed whether or not anything was suppressed: a run that silently
    dropped candidates would be indistinguishable from a run that found
    fewer of them, and a ledger that is read but never mentioned is only
    marginally better than one nothing reads at all.
    """
    lines = []
    lines.append(
        "ledger: {} -- {} decision(s) read, {} group(s) suppressed".format(
            ledger_path, len(entries), len(suppressed)
        )
    )
    for obs_ids, entry in sorted(suppressed, key=lambda s: sorted(s[0])):
        lines.append(
            "  obs {} -- rejected {}: {}".format(
                ",".join(str(i) for i in sorted(obs_ids)), entry.date, entry.note
            )
        )
    if suppressed:
        lines.append(
            "  (matched on the observation ids, not on a group id: group ids "
            "are per-run)"
        )
    for lineno, raw, reason in malformed:
        lines.append(
            "  ignored malformed ledger line {}: {} -- {!r}".format(
                lineno, reason, raw
            )
        )
    return "\n".join(lines)


def format_report(groups, total_before_limit, min_projects):
    lines = []
    if not groups:
        lines.append(
            "no candidate groups met the thresholds "
            "(need >= {} distinct project(s))".format(min_projects)
        )
        lines.append(HONESTY_NOTE)
        return "\n".join(lines)

    lines.append(
        "{} candidate group(s) met the thresholds; showing {}".format(
            total_before_limit, len(groups)
        )
    )
    lines.append("")
    for g in groups:
        lines.append(
            "group {}: {} distinct project(s), {} observation(s)".format(
                g["group_id"], len(g["projects"]), len(g["members"])
            )
        )
        lines.append("  projects: {}".format(", ".join(g["projects"])))
        lines.append("  date span: {} to {}".format(*g["date_span"]))
        lines.append("  members:")
        for m in g["members"]:
            excerpt = m.text[:200].replace("\n", " ").strip()
            suffix = "..." if len(m.text) > 200 else ""
            lines.append(
                "    - [{}] {} obs #{} \"{}\": {}{}".format(
                    m.project, m.created_at[:10] or "?", m.obs_id, m.title, excerpt, suffix
                )
            )
        lines.append(
            "  terms in common: {}".format(
                ", ".join(g["common_terms"]) if g["common_terms"] else "(none shared by 2+ members)"
            )
        )
        lines.append("")

    lines.append(HONESTY_NOTE)
    return "\n".join(lines)


def render_draft(g):
    lines = []
    lines.append("# TODO: name the class")
    lines.append("")
    lines.append("**Trigger:** TODO -- describe when a human should open this page")
    lines.append("")
    lines.append("**Trigger paths:**")
    lines.append("")
    lines.append("**Class:** TODO -- one line, what goes wrong")
    lines.append("")
    lines.append("## What goes wrong")
    lines.append("")
    lines.append("TODO")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    lines.append(
        "candidate group {} -- {} project(s), {} observation(s), {} to {} "
        "(mined, not reviewed):".format(
            g["group_id"], len(g["projects"]), len(g["members"]), *g["date_span"]
        )
    )
    lines.append("")
    for m in g["members"]:
        lines.append(
            "`{}`, observation #{}, {} -- \"{}\":".format(
                m.project, m.obs_id, m.created_at[:10] or "?", m.title
            )
        )
        lines.append("")
        for text_line in (m.text.splitlines() or [""]):
            lines.append("    " + text_line)
        lines.append("")
    lines.append("## Check before you trust this")
    lines.append("")
    lines.append("- TODO")
    lines.append("")
    lines.append(
        "<!-- terms this group had in common, for reference only, not evidence: {} -->".format(
            ", ".join(g["common_terms"]) if g["common_terms"] else "(none)"
        )
    )
    lines.append("")
    # The ids this draft came from, in the shape the ledger is keyed by, so
    # recording the decision is a copy and not a reconstruction. Group ids
    # are per-run and mean nothing tomorrow; these ids do not move.
    lines.append("<!-- ledger line for this group, once you decide:")
    lines.append(
        "- YYYY-MM-DD | admitted | TODO-page-name | obs: {} | TODO why".format(
            ",".join(str(m.obs_id) for m in sorted(g["members"], key=lambda m: m.obs_id))
        )
    )
    lines.append(
        "     ({} are the three decisions; keep the obs ids exactly as written,".format(
            "/".join(LEDGER_DECISIONS)
        )
    )
    lines.append("     they are the key a rejected group is recognised by next run) -->")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Mine the engram store's **Learned** lessons for candidate "
            "precedent pattern pages. Proposes; a human disposes."
        ),
        epilog=(
            "Group ids are assigned by rank within one invocation and are "
            "only comparable across runs that share --db, --min-projects "
            "and --similarity. The --ledger file is therefore keyed by "
            "observation id, not by group id, and suppressing a group "
            "shifts the ids of the ones still shown. Ledger format, one "
            "decision per line:\n" + LEDGER_FORMAT
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="path to engram.db (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=15, help="max groups to report (default: 15)")
    parser.add_argument("--min-projects", type=int, default=2, dest="min_projects", help="minimum distinct projects per group (default: 2)")
    parser.add_argument("--similarity", type=float, default=0.25, help="Jaccard threshold for grouping (default: 0.25)")
    parser.add_argument("--draft", type=int, default=None, metavar="GROUP_ID", help="print a skeleton pattern page for this group id instead of a report")
    parser.add_argument("--ledger", default=None, metavar="PATH", help="read an append-only decision ledger and skip any group whose observation ids were already rejected in it")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    db_path = Path(args.db).expanduser()

    if not db_path.exists():
        print("error: no database at {}".format(db_path), file=sys.stderr)
        return 1

    ledger_entries = []
    ledger_malformed = []
    rejected = {}
    ledger_path = None
    if args.ledger:
        ledger_path = Path(args.ledger).expanduser()
        # A --ledger that points at nothing is an error, not a quiet
        # no-op: a ledger silently not read is the exact failure this
        # flag exists to stop.
        try:
            ledger_entries, ledger_malformed = load_ledger(ledger_path)
        except OSError as exc:
            print(
                "error: could not read ledger {}: {}".format(ledger_path, exc),
                file=sys.stderr,
            )
            return 1
        rejected = rejected_obs_sets(ledger_entries)

    try:
        con = open_readonly(db_path)
    except sqlite3.OperationalError as exc:
        print("error: could not open {} read-only: {}".format(db_path, exc), file=sys.stderr)
        return 1

    try:
        verify_schema(con)
        lessons = read_lessons(con)
    finally:
        con.close()

    groups, suppressed = assemble_groups(
        lessons, args.similarity, args.min_projects, rejected
    )

    if args.draft is not None:
        # In draft mode stdout is a pattern page and nothing else, so
        # anything the ledger has to say goes to stderr rather than into
        # the middle of the page.
        if ledger_path is not None:
            print(
                format_ledger_report(
                    ledger_path, ledger_entries, suppressed, ledger_malformed
                ),
                file=sys.stderr,
            )
        group = find_group(groups, args.draft)
        if group is None:
            print(
                "error: no group with id {} under these thresholds "
                "(--min-projects {}, --similarity {}{}); "
                "run without --draft first to see current group ids".format(
                    args.draft,
                    args.min_projects,
                    args.similarity,
                    ", and {} group(s) suppressed by the ledger".format(len(suppressed))
                    if suppressed
                    else "",
                ),
                file=sys.stderr,
            )
            return 1
        print(render_draft(group))
        return 0

    total = len(groups)
    print(format_report(groups[: args.limit], total, args.min_projects))
    if ledger_path is not None:
        print("")
        print(
            format_ledger_report(
                ledger_path, ledger_entries, suppressed, ledger_malformed
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
