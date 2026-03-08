"""Category normalization — maps freeform categories to canonical set."""

CANONICAL_CATEGORIES = frozenset({
    "architecture",
    "debugging",
    "deployment",
    "infrastructure",
    "patterns",
    "tooling",
    "security",
    "frontend",
    "backend",
    "data",
    "business",
    "documentation",
    "session-log",
    "insight",
    "_archived",
})

# Maps known freeform categories to canonical ones.
# Unknown categories are normalized via heuristics (lowercase, strip).
_CATEGORY_MAP = {
    "learned-pattern": "patterns",
    "patterns": "patterns",
    "pattern": "patterns",
    "development_pattern": "patterns",
    "Patterns, Configurations, or Commands": "patterns",
    "Patterns, configurations, or commands that could be reused": "patterns",
    "Recipe Pattern": "patterns",
    "software development": "patterns",
    "Best Practices": "patterns",
    "tooling": "tooling",
    "scripting": "tooling",
    "Scripting": "tooling",
    "Command": "tooling",
    "tool": "tooling",
    "Knowledge Management": "tooling",
    "Code Review & Process Compliance": "tooling",
    "testing": "tooling",
    "Testing": "tooling",
    "asset management": "tooling",
    "dependency management": "tooling",
    "infrastructure": "infrastructure",
    "system_configuration": "infrastructure",
    "system_optimization": "infrastructure",
    "Configuration": "infrastructure",
    "configuration": "infrastructure",
    "configuration_management": "infrastructure",
    "Config": "infrastructure",
    "technical_configuration": "infrastructure",
    "system_administration": "infrastructure",
    "system_customization": "infrastructure",
    "system_integration": "infrastructure",
    "map-configuration": "infrastructure",
    "icon-configuration": "infrastructure",
    "URL Configuration": "infrastructure",
    "integration": "infrastructure",
    "desktop-customization": "infrastructure",
    "debugging": "debugging",
    "Debugging": "debugging",
    "gotchas": "debugging",
    "Solutions to Problems": "debugging",
    "Solutions to problems": "debugging",
    "solution": "debugging",
    "Solution": "debugging",
    "troubleshooting": "debugging",
    "Error Handling": "debugging",
    "frontend": "frontend",
    "Frontend": "frontend",
    "HTML": "frontend",
    "JavaScript": "frontend",
    "CSS": "frontend",
    "CSS Syntax": "frontend",
    "CSS Scaling": "frontend",
    "Responsive Design": "frontend",
    "styling": "frontend",
    "elementor": "frontend",
    "Font Management": "frontend",
    "HTML/CSS": "frontend",
    "HTML Meta Tags": "frontend",
    "user-preferences": "frontend",
    "insight": "insight",
    "instinct": "insight",
    "research": "insight",
    "contradiction": "insight",
    "general": "insight",
    "architecture": "architecture",
    "Technical Decisions and Rationale": "architecture",
    "Technical decisions and their rationale": "architecture",
    "Technical Decision and Rationale": "architecture",
    "deployment": "deployment",
    "ops": "deployment",
    "business": "business",
    "personal": "business",
    "creative": "business",
    "project": "business",
    "productivity": "business",
    "strategy": "business",
    "data": "data",
    "ml-training": "data",
    "labeling": "data",
    "Home Health Testing": "data",
    "data-visualization": "data",
    "Database": "data",
    "Biomarker Analysis": "data",
    "geometry": "data",
    "Ingredient Pairing": "data",
    "Ingredient Use": "data",
    "backend": "backend",
    "Drupal": "backend",
    "URL Construction": "backend",
    "module": "backend",
    "technical development": "backend",
    "Performance Optimization": "backend",
    "documentation": "documentation",
    "project-docs": "documentation",
    "reference": "documentation",
    "technical documentation improvement": "documentation",
    "documentation & setup optimization": "documentation",
    "session-log": "session-log",
    "development_activity": "session-log",
    "development_workflow": "session-log",
    "security": "security",
    "_archived": "_archived",
}


def normalize_category(category: str | None) -> str | None:
    """Normalize a category string to a canonical value.

    Returns None if input is None.
    Known categories are mapped directly.
    Unknown categories are lowercased and returned as-is (they'll be caught
    in the next normalization pass).
    """
    if category is None:
        return None
    # Direct lookup
    canonical = _CATEGORY_MAP.get(category)
    if canonical:
        return canonical
    # Already canonical
    lower = category.lower().strip()
    if lower in CANONICAL_CATEGORIES:
        return lower
    # Return lowercased — will be visible in stats as uncategorized
    return lower
