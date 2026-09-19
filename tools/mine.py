#!/usr/bin/env python3
"""Offline miner over the engram memory store.

`precedent`'s pattern pages (patterns/*.md) are written by hand today. The
raw material for more of them already sits in a third-party tool's local
SQLite store: engram, at ~/.engram/engram.db, keeps every "**Learned**"
lesson anyone has ever had it save, across every project it has touched.
Nobody reads that back before starting new work -- see DESIGN.md, question 8,
for the two probes that show *why* (the lessons are lexically un-findable
and nobody thinks to search for a class before it is one).

This script reads that store read-only, pulls out the **Learned** section of
each qualifying observation, groups lessons that use similar words, and
prints candidate groups ranked by how many distinct projects they span. It
proposes; a human disposes -- see the closing note this script always
prints, and DESIGN.md question 1 for why that gate exists at all.

Usage:
    python3 tools/mine.py                          # report, defaults
    python3 tools/mine.py --limit 10 --min-projects 2
    python3 tools/mine.py --draft 1                # skeleton page for group 1

Group ids are only stable across invocations that use the same --db,
--min-projects and --similarity: they are assigned by rank within one run,
not persisted anywhere.
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


def assemble_groups(lessons, threshold, min_projects):
    member_map = build_groups(lessons, threshold)
    groups = [group_stats(lessons, idxs) for idxs in member_map.values()]
    groups = [g for g in groups if len(g["projects"]) >= min_projects]

    # ------------------------------------------------------------------
    # Rank by the number of DISTINCT PROJECTS a group spans, descending,
    # then by size. This ordering is the whole point of the tool and comes
    # straight from evidence/measurement-2026-09-18.md: a lesson repeated
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
    return groups


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
            "and --similarity."
        ),
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="path to engram.db (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=15, help="max groups to report (default: 15)")
    parser.add_argument("--min-projects", type=int, default=2, dest="min_projects", help="minimum distinct projects per group (default: 2)")
    parser.add_argument("--similarity", type=float, default=0.25, help="Jaccard threshold for grouping (default: 0.25)")
    parser.add_argument("--draft", type=int, default=None, metavar="GROUP_ID", help="print a skeleton pattern page for this group id instead of a report")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    db_path = Path(args.db).expanduser()

    if not db_path.exists():
        print("error: no database at {}".format(db_path), file=sys.stderr)
        return 1

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

    groups = assemble_groups(lessons, args.similarity, args.min_projects)

    if args.draft is not None:
        group = find_group(groups, args.draft)
        if group is None:
            print(
                "error: no group with id {} under these thresholds "
                "(--min-projects {}, --similarity {}); "
                "run without --draft first to see current group ids".format(
                    args.draft, args.min_projects, args.similarity
                ),
                file=sys.stderr,
            )
            return 1
        print(render_draft(group))
        return 0

    total = len(groups)
    print(format_report(groups[: args.limit], total, args.min_projects))
    return 0


if __name__ == "__main__":
    sys.exit(main())
