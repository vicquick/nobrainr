"""Tag canonicalization — unit tests for the normalizer."""

from nobrainr.utils.tags import canonicalize_tag, canonicalize_tags


def test_lowercases():
    assert canonicalize_tag("Python") == "python"
    assert canonicalize_tag("IFC") == "ifc"
    assert canonicalize_tag("Vectorworks") == "vectorworks"


def test_space_to_hyphen():
    assert canonicalize_tag("data transformation") == "data-transformation"
    assert canonicalize_tag("  data  cleaning  ") == "data-cleaning"


def test_underscore_to_hyphen():
    assert canonicalize_tag("data_transformation") == "data-transformation"


def test_plural_aliases():
    assert canonicalize_tag("lessons") == "lesson"
    assert canonicalize_tag("Lessons") == "lesson"
    assert canonicalize_tag("lessons learned") == "lesson"
    assert canonicalize_tag("lessons-learned") == "lesson"
    assert canonicalize_tag("Incidents") == "incident"


def test_postmortem_collapse():
    assert canonicalize_tag("postmortem") == "postmortem"
    assert canonicalize_tag("postmortem-lesson") == "postmortem"
    assert canonicalize_tag("postmortem-application") == "postmortem"
    assert canonicalize_tag("post-mortem") == "postmortem"


def test_preserve_filename_tags():
    # Tags that point at a file or path carry real signal — keep them.
    assert canonicalize_tag("helpers.py") == "helpers.py"
    assert canonicalize_tag("README.md") == "readme.md"
    assert canonicalize_tag("requirements.txt") == "requirements.txt"


def test_preserve_path_tags():
    assert canonicalize_tag("feature/knowledge-crawl") == "feature/knowledge-crawl"
    assert canonicalize_tag("agent/test/ci-fix") == "agent/test/ci-fix"


def test_preserve_namespace_tags():
    assert canonicalize_tag("color:red") == "color:red"
    assert canonicalize_tag("handoff:open") == "handoff:open"


def test_preserve_unicode():
    # German umlauts survive — already lowercase via str.lower().
    assert canonicalize_tag("Länge") == "länge"


def test_empty_and_whitespace():
    assert canonicalize_tag("") == ""
    assert canonicalize_tag("   ") == ""
    assert canonicalize_tag(None) == ""  # type: ignore[arg-type]


def test_canonicalize_tags_deduplicates():
    # "Python" and "python" collapse; order preserved.
    assert canonicalize_tags(["Python", "ifc", "python", "IFC"]) == ["python", "ifc"]


def test_canonicalize_tags_drops_empty():
    assert canonicalize_tags(["", "  ", "python"]) == ["python"]


def test_canonicalize_tags_none():
    assert canonicalize_tags(None) == []
    assert canonicalize_tags([]) == []
