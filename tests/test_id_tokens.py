"""Unit tests for ID-token extraction used in the hybrid-search literal branch."""

from nobrainr.utils.id_tokens import extract_id_tokens


def test_empty():
    assert extract_id_tokens("") == []
    assert extract_id_tokens(None) == []  # type: ignore[arg-type]


def test_plain_query_no_ids():
    # No hashes, no issue numbers — extractor must return empty.
    assert extract_id_tokens("how do I use docker compose") == []


def test_git_short_hash():
    # 7-char short hash, mixed with prose.
    assert extract_id_tokens("commit 798461a6 fix import path") == ["798461a6"]


def test_git_full_hash():
    full = "d4e5f6a78b901234567890abcdef123456789012"
    assert extract_id_tokens(f"see {full} for context") == [full]


def test_all_alpha_hex_excluded():
    # "feeded" is 6 chars anyway but "deadbeef" is 8 chars and all-alpha.
    # Require at least one digit to avoid matching English words.
    # "deadbeef" has no digits → excluded. Good.
    assert extract_id_tokens("The deadbeef pattern is old") == []


def test_hex_with_one_digit_included():
    # "a1bcdef" — 7 chars, has a digit → should be extracted.
    assert extract_id_tokens("commit a1bcdef fix") == ["a1bcdef"]


def test_issue_number():
    assert extract_id_tokens("Issue 175 Master LV") == ["175"]
    assert extract_id_tokens("fix for issue #42") == ["42"]


def test_pr_number():
    assert extract_id_tokens("PR 147 Qwen3-14B AI") == ["147"]
    assert extract_id_tokens("pull 92 review") == ["92"]


def test_bare_hash_in_prose_not_mistaken_for_issue():
    # Bare number without keyword is NOT extracted — otherwise every
    # "Python 3.14" mention would trigger a literal match.
    assert extract_id_tokens("Python 3.14 docs") == []


def test_full_uuid_preserved():
    # Full UUID must come back whole — hex-hash regex would otherwise
    # fragment its first 8 chars and the hyphenated remainder.
    uid = "019d01a4-6052-7597-8585-910ed2dff10d"
    assert uid in extract_id_tokens(f"memory {uid} please")


def test_bare_hash_without_keyword_matches():
    # # followed by digits should match even without a preceding word
    # boundary ("fixes #42" — space-to-# is not a \b transition).
    assert extract_id_tokens("fixes #42 today") == ["42"]


def test_deduplicates():
    q = "commit 798461a6 references 798461a6 twice"
    assert extract_id_tokens(q) == ["798461a6"]


def test_multiple_tokens():
    q = "PR 147 introduces commit a1b2c3d and fixes #42"
    tokens = extract_id_tokens(q)
    assert "a1b2c3d" in tokens
    assert "147" in tokens
    assert "42" in tokens
