"""Search tag filters must match the canonical form store_memory writes."""

from nobrainr.db.queries import _tag_filter_values
from nobrainr.utils.tags import canonicalize_tags


def test_underscore_and_case_variants_match_stored_form():
    stored = canonicalize_tags(["0862e8bf_abs", "Python", "lessons"])
    values = _tag_filter_values(["0862e8bf_abs", "Python", "lessons"])
    assert set(stored) <= set(values)


def test_raw_forms_kept_for_legacy_rows():
    values = _tag_filter_values(["My_Tag"])
    assert values == ["My_Tag", "my-tag"]


def test_already_canonical_not_duplicated():
    assert _tag_filter_values(["nobrainr", "lesson"]) == ["nobrainr", "lesson"]
