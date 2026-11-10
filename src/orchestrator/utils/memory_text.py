"""Deterministic text analysis for shared-memory triage.

Pure functions only: no database, no git, no provider, no clock. Everything here either
decides what changes the *injected prompt* (selection order) or produces a report, so every
predicate must be stable across calls and reproducible from entry content alone. Live
checks (missing paths, merged branches, project config) belong in
`services/memory_triage.py`, which is report-only and never mutates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Words that carry a fact, not sentence shape. Without this, overlap collapses on grammar.
_STOP = frozenset(
    """the and for with that this from are was were will have has not but its into than then
    them they there here which what when how why also must should could would because before
    after over under you your ours their been being some any each other none both only
    just like same very much many more most such rather instead unless while since""".split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{3,}")
# A bare commit SHA: 7-40 hex characters containing at least one digit. The digit
# requirement avoids matching words such as "acceded" or "decaffeinated".
_SHA = re.compile(r"\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[0-9])[0-9a-f]+\b")
_TASK_SHAPE = re.compile(
    r"\bT-?\d{1,3}\b|\bR-\d{1,3}\b|\bround\s+\d{1,3}\b|\btask\s*#?\d{1,3}\b"
    r"|\bphase\s+\d{1,3}\b|\btask-\d{1,3}\b",
    re.I,
)
_FORWARD = re.compile(
    r"\b(pending merge|not yet merged|unmerged|will be|will land|will merge|is planned|"
    r"planned|not implemented|for now|TODO|in progress|work in progress)\b",
    re.I,
)
_RELATIVE = re.compile(
    r"\b(currently|today|yesterday|this week|last week|as of|measured|recently|now that)\b",
    re.I,
)
_ABS_DATE = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\bweek\s+\d{1,2}\b", re.I
)
_IMPERATIVE = re.compile(
    r"\b(you must|you should|do not|don't|never |always |make sure|remember to)\b", re.I
)
_MULTI_FACT = re.compile(r"\(\d\)|\b(?:first|second|third|fourth),")

# Code-ish entities: backticked spans, call shapes, paths, files.
_ENTITY = re.compile(
    r"`[^`]{2,60}`"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*\(\)"
    r"|\b[A-Za-z_][A-Za-z0-9_\-]*(?:/[A-Za-z0-9_\-<>.]+)+/?"
    r"|\b[A-Za-z_][A-Za-z0-9_\-]*\.(?:py|md|sh|ts|tsx|js|jsx|json|sql|toml|html|txt|log|lock)\b"
)

# ---------------------------------------------------------------- subject dedup
#
# Rare-word containment alone is not enough on this corpus: the operator measured that the
# pending pile restates the same SUBJECT (a code symbol, a file, a CLI flag, a rule) in
# different words without the rare word sets lining up ("three entries restate the evidence
# freshness rule", "four restate the diff floor arithmetic"). Whole-text similarity is worse
# still -- a bag-of-words ratio flagged 461 of 595 pending pairs at 0.72 while the true
# duplicate count was zero -- so subject grouping is keyed on IDENTIFIER-shaped terms, not on
# prose overlap, and it is star-shaped rather than transitive (see `subject_clusters`).

#: A pair of entries is the same fact when the smaller rare-word set is at least half contained.
DUPLICATE_CONTAINMENT_THRESHOLD = 0.5
#: Identifier terms in more than this many entries are domain furniture (`verify-evidence`,
#: `handoff.md`, `coding-orchestrator`), not a subject: sharing them must not link two rows.
SUBJECT_MAX_DF = 6
#: Two rows are the same subject when they share at least this many RARE identifier terms.
SUBJECT_MIN_SHARED = 2
#: A dotted name, a hyphenated compound, a path or a filename: `prompt_builder._floor_head`,
#: `evidence-freshness`, `verify-evidence/<task>`, `timing.txt`.
_IDENT_COMPOUND = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.\-/][A-Za-z0-9<>]+)+")
#: A CLI flag such as `--at-tip` or `--check` is a subject the operator can search for.
_CLI_FLAG = re.compile(r"--[a-z][a-z0-9\-]{2,}")
_BACKTICK = re.compile(r"`([^`]{2,80})`")

ROT_FLAGS: tuple[tuple[str, str], ...] = (
    ("task_shape", "names a task/phase/round - true only while that task is open"),
    ("bare_sha", "contains a commit SHA - rot-prone and unreadable once history moves"),
    ("forward_looking", "asserts future state ('pending merge', 'planned') - goes stale silently"),
    ("absolute_date", "carries a date - belongs in the build log, not in every prompt"),
    ("relative_time", "says 'currently'/'measured' - unverifiable later"),
    ("multi_fact", "packs several facts into one entry - cannot be edited or retracted singly"),
    ("imperative", "instruction-shaped text - memory is injected into every future prompt"),
)


def content_words(text: str) -> set[str]:
    """Lowercased content words: the signal for 'same fact restated'."""
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


_PLACEHOLDER = re.compile(r"\{[^}]*\}|<[^>]*>|\*\*?|\.\.\.")


def entity_terms(text: str) -> set[str]:
    """Code identifiers, paths and backticked names: the signal for 'same subject'."""
    out: set[str] = set()
    for m in _ENTITY.finditer(_PLACEHOLDER.sub(" ", text or "")):
        s = m.group(0).strip("`").strip()
        s = s.lower().rstrip("():/ ,.")
        if len(s) > 3 and not s.isdigit():
            out.add(s)
    return out


def rot_flags(text: str) -> list[str]:
    """Stable content-derived staleness risks. Never a verdict - a warning only."""
    t = text or ""
    flags = []
    if _TASK_SHAPE.search(t):
        flags.append("task_shape")
    if _SHA.search(t):
        flags.append("bare_sha")
    if _FORWARD.search(t):
        flags.append("forward_looking")
    if _ABS_DATE.search(t):
        flags.append("absolute_date")
    if _RELATIVE.search(t):
        flags.append("relative_time")
    if len(_MULTI_FACT.findall(t)) >= 2:
        flags.append("multi_fact")
    if len(_IMPERATIVE.findall(t)) >= 3:
        flags.append("imperative")
    return flags


def is_rot_prone(text: str) -> bool:
    """Task-shaped or SHA-bearing entries expire when the task closes."""
    f = set(rot_flags(text))
    return bool({"task_shape", "bare_sha", "forward_looking"} & f)


def referenced_paths(text: str) -> list[str]:
    """Paths an entry claims exist, in first-seen order, **original case preserved**.

    Case matters: the claim is tested with a git pathspec, which is case-sensitive. Only
    slashed paths count — a bare `adapters.json` or an extension fragment left over from
    `*.log` is not a claim about repository layout, and a check that flags them stops being
    trusted. Shell brace expansions and placeholders (`web/{src,index.html}`, `runs/<id>/`,
    `*.txt`) are blanked before extraction instead of yielding bogus names.
    """
    out: list[str] = []
    for m in _ENTITY.finditer(_PLACEHOLDER.sub(" ", text or "")):
        s = m.group(0).strip("`").strip().rstrip("():,/ .")
        if len(s) < 4 or s.isdigit() or " " in s or "/" not in s:
            continue
        if s.startswith(".") or not re.search(r"[A-Za-z0-9]\.?", s.split("/")[0]):
            continue
        if s not in out:
            out.append(s)
    return out


def token_containment(shared: set[str], size_a: int, size_b: int) -> float:
    """Fraction of the smaller set covered by the intersection. 1.0 means nested facts."""
    smallest = min(size_a, size_b)
    return (shared / smallest) if smallest else 0.0


def build_df(texts: Iterable[str]) -> dict[str, int]:
    """Document frequency per content word: the basis for 'rare' (distinctive) tokens."""
    df: dict[str, int] = {}
    for t in texts:
        for w in content_words(t):
            df[w] = df.get(w, 0) + 1
    return df


def rare_words(text: str, df: dict[str, int], max_df: int = 3) -> set[str]:
    """Words appearing in at most `max_df` entries - these identify the specific fact."""
    return {w for w in content_words(text) if df.get(w, 0) <= max_df}


def duplicate_detail(
    a: str, b: str, df: dict[str, int], *, threshold: float = DUPLICATE_CONTAINMENT_THRESHOLD
) -> tuple[bool, float, float]:
    """(is_duplicate, containment, threshold) -- the threshold is data, not prose.

    A reason string must name the comparison that produced it, so the caller builds it from
    this returned threshold instead of hardcoding a copy that can drift from the matcher.
    """
    ra, rb = rare_words(a, df), rare_words(b, df)
    if not ra or not rb:
        return False, 0.0, threshold
    shared = len(ra & rb)
    c = token_containment(shared, len(ra), len(rb))
    return c >= threshold, round(c, 3), threshold


def is_duplicate_pair(
    a: str, b: str, df: dict[str, int], *, threshold: float = DUPLICATE_CONTAINMENT_THRESHOLD
) -> tuple[bool, float]:
    """True when one entry's rare words are ~fully contained in the other's.

    Bag-of-words ratios are useless on this corpus (every pair shares the domain
    vocabulary); rare-token containment is what separates 'same fact, new wording' from
    'different fact, same subject'.
    """
    is_dup, ratio, _threshold = duplicate_detail(a, b, df, threshold=threshold)
    return is_dup, ratio


def duplicate_reason(ratio: float, threshold: float) -> str:
    """The recorded reason for a rare-token link, derived from the matcher's own numbers."""
    return (
        f"rare-token containment {ratio:.3f} >= {threshold:.2f} identifies the same fact "
        "in new words"
    )


def subject_terms(text: str) -> set[str]:
    """Identifier-shaped subject terms: code symbols, paths, filenames and CLI flags.

    Deliberately not prose words: `prompt_builder` and `--at-tip` name a subject, while
    `the`, `diff` and `file` only name the domain. Backticked spans keep their whole form
    (``orchestrator task evidence <id> --at-tip``) so a command-shaped subject matches as
    one term, and a dotted/path compound also contributes the parts that can be searched
    (``prompt_builder._floor_head`` -> ``prompt_builder``, ``_floor_head``).
    """
    out: set[str] = set()
    for span in _BACKTICK.findall(text or ""):
        span = span.strip().lower().rstrip(".,;:)")
        if len(span) > 3 and re.search(r"[_.\-/]", span):
            out.add(span)
    for compound in _IDENT_COMPOUND.findall(text or ""):
        name = compound.lower().rstrip("/")
        if len(name) > 3:
            out.add(name)
            for part in re.split(r"[_.\-/]", name):
                if len(part) > 3 and not part.isdigit():
                    out.add(part)
    for flag in _CLI_FLAG.findall(text or ""):
        out.add(flag.lower())
    return out


def build_term_df(texts: Iterable[str]) -> dict[str, int]:
    """Document frequency of identifier terms, the basis for 'rare identifier'."""
    df: dict[str, int] = {}
    for text in texts:
        for term in subject_terms(text):
            df[term] = df.get(term, 0) + 1
    return df


def rare_subject_terms(
    text: str, tdf: dict[str, int], *, max_df: int = SUBJECT_MAX_DF
) -> set[str]:
    """Identifier terms specific enough to identify a subject rather than the domain."""
    return {t for t in subject_terms(text) if tdf.get(t, 0) <= max_df}


def is_subject_pair(
    a: str,
    b: str,
    tdf: dict[str, int],
    *,
    max_df: int = SUBJECT_MAX_DF,
    min_shared: int = SUBJECT_MIN_SHARED,
) -> tuple[bool, tuple[str, ...]]:
    """(is_same_subject, shared rare identifiers). Never a whole-text ratio."""
    shared = rare_subject_terms(a, tdf, max_df=max_df) & rare_subject_terms(
        b, tdf, max_df=max_df
    )
    return len(shared) >= min_shared, tuple(sorted(shared))


def subject_reason(shared: Iterable[str]) -> str:
    """The recorded reason for a subject link, derived from the actual shared terms."""
    terms = list(shared)
    return (
        f"shares {len(terms)} rare identifier term(s) "
        f"({', '.join(terms[:5])}) with the existing entry; subject duplicate, not a "
        "whole-text ratio"
    )


def subject_clusters(
    entries: list,
    *,
    max_df: int = SUBJECT_MAX_DF,
    min_shared: int = SUBJECT_MIN_SHARED,
) -> list[list[int]]:
    """Star-shaped subject clusters: each member matches the anchor, never a chain.

    `entries` must already be in value order (highest first) -- the anchor is the row the
    caller would keep, so a pinned row can never be absorbed into a weaker one. Transitive
    entity unions are deliberately NOT used: A-B and B-C still leaves A and C apart, which
    is the false merge the operator's live set exposed (two unrelated facts sharing only
    `handoff.json`).
    """
    usable = [e for e in entries if getattr(e, "id", None) is not None]
    if len(usable) < 2:
        return []
    tdf = build_term_df([e.content for e in usable])
    rare = {e.id: rare_subject_terms(e.content, tdf, max_df=max_df) for e in usable}
    assigned: set[int] = set()
    groups: list[list[int]] = []
    for anchor in usable:
        if anchor.id in assigned:
            continue
        members = [anchor.id]
        for other in usable:
            if other.id == anchor.id or other.id in assigned:
                continue
            if len(rare[anchor.id] & rare[other.id]) >= min_shared:
                members.append(other.id)
        if len(members) > 1:
            assigned.update(members)
            groups.append(sorted(members))
    return sorted(groups, key=lambda g: g[0])
