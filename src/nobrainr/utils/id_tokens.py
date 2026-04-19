"""Extract high-signal ID tokens from a search query.

Queries like "commit 798461a6" or "Issue 175" or "PR 147 Qwen3-14B"
contain literal anchors that mean a specific memory. Vector search
doesn't care about the bytes of "798461a6" — it just sees another
short token — so commit-hash queries score poorly. Lexical tokens pass
them through a third search branch that does literal substring match,
which then joins the RRF fusion alongside vector + FTS.

Kept tight: we ONLY extract tokens that are *confidently* identifiers,
to avoid flooding the literal branch with common words. Hex hashes
need 7+ chars; issue/PR numbers need a preceding keyword or `#`.
"""

from __future__ import annotations

import re

# Git short-hashes are 7 chars minimum, full hashes 40. Require at least
# one digit to avoid matching English words that happen to be 7+ letters
# (a-f only), e.g. "feeded". Restricting to `hex with at least one digit`
# means "defaced" won't match but "deadbeef" will.
_HEX_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
_HAS_DIGIT = re.compile(r"\d")

# "issue 175", "Issue #175", "PR 147", "pull 92", "#42" — capture the
# number. Require a keyword or # prefix so bare 3-digit numbers in
# prose don't flood the literal branch. `#` matches at any position
# since it's not a word character — a preceding \b wouldn't match for
# the pattern "fixes #42" (space-to-# is not a word boundary).
_ISSUE_PR = re.compile(
    r"(?:\b(?:issue|pr|pull)\s*#?|#)(\d{2,6})\b",
    re.IGNORECASE,
)

# Full UUID (8-4-4-4-12) or any meaningful prefix that still has at
# least one dash. Matched FIRST so its hex bytes aren't fragmented by
# the plain hex-hash regex.
_UUID_FULL = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_UUID_PREFIX = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){1,3}\b",
    re.IGNORECASE,
)


def extract_id_tokens(query: str) -> list[str]:
    """Return high-signal literal tokens found in the query.

    Result is de-duplicated + order preserved so callers can rank them.
    Returns empty list if the query contains no ID-like tokens.

    Detection order: full UUID → UUID prefix → hex hash → issue/PR
    number. UUIDs are matched first because the hex-hash regex would
    otherwise fragment their first 8 chars into a standalone "hash".
    """
    if not query:
        return []
    seen: set[str] = set()
    out: list[str] = []
    # Work on a scratch copy so we can mask out matched UUID spans
    # before the shorter hex-hash regex runs.
    lowered = query.lower()
    scratch = lowered

    for m in _UUID_FULL.finditer(lowered):
        token = m.group(0)
        if token not in seen:
            seen.add(token)
            out.append(token)
        # Blank out the span so hex-hash won't re-match these bytes.
        scratch = scratch.replace(token, " " * len(token), 1)

    for m in _UUID_PREFIX.finditer(scratch):
        token = m.group(0)
        if token not in seen:
            seen.add(token)
            out.append(token)
        scratch = scratch.replace(token, " " * len(token), 1)

    for m in _HEX_HASH.finditer(scratch):
        token = m.group(0)
        if not _HAS_DIGIT.search(token):
            continue  # all-alpha hex like "feeded" — probably a word
        if token not in seen:
            seen.add(token)
            out.append(token)

    for m in _ISSUE_PR.finditer(query):
        num = m.group(1)
        if num not in seen:
            seen.add(num)
            out.append(num)

    # Drop tokens shorter than 3 chars — they trigger a full-table ILIKE
    # scan and match too broadly ("42" matches every content with that
    # digit sequence). A too-short token is a false signal, not a hit.
    return [t for t in out if len(t) >= 3]
