"""Input sanitization and prompt injection detection for chat."""

import re
import unicodedata


def sanitize_user_input(text: str, max_length: int = 2000) -> str:
    """Strip dangerous characters and limit length."""
    # Normalize unicode to prevent homoglyph attacks
    text = unicodedata.normalize("NFC", text)
    # Strip null bytes and non-printable control chars (keep newlines, tabs)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    # Strip zero-width characters
    text = re.sub(r"[\u200b-\u200f\u2028-\u202f\u2060\ufeff]", "", text)
    return text.strip()[:max_length]


def sanitize_context(text: str, max_length: int = 500) -> str:
    """Sanitize memory content before injecting into LLM context.

    Escapes patterns that could be interpreted as prompt instructions.
    """
    text = sanitize_user_input(text, max_length)
    # Escape sequences that look like role markers or instructions
    text = re.sub(
        r"(?i)^(system|user|assistant|human|instructions?|rules?)\s*:",
        r"[\1]:",
        text,
        flags=re.MULTILINE,
    )
    return text


# Patterns that indicate injection attempts
_INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?previous\s+(instructions?|prompts?|rules?)",
    r"(?i)ignore\s+(the\s+)?(above|system|prior)\s+",
    r"(?i)you\s+are\s+now\s+",
    r"(?i)new\s+instructions?\s*:",
    r"(?i)forget\s+(all\s+)?(your\s+)?(instructions?|rules?|constraints?)",
    r"(?i)override\s+(system|safety|security)",
    r"(?i)jailbreak",
    r"(?i)do\s+anything\s+now",
    r"(?i)act\s+as\s+(if\s+you\s+are\s+|a\s+)?(?!the\s+knowledge)",
    r"(?i)pretend\s+(to\s+be|you\s+are)",
    r"(?i)bypass\s+(your\s+)?(restrictions?|rules?|filters?)",
    r"(?i)reveal\s+(your\s+)?(system\s+prompt|instructions?)",
    r"(?i)what\s+(is|are)\s+your\s+(system\s+)?instructions?",
    r"(?i)repeat\s+(the\s+)?(system\s+)?prompt",
    r"(?i)print\s+(your\s+)?(system\s+)?prompt",
]
_COMPILED_PATTERNS = [re.compile(p) for p in _INJECTION_PATTERNS]


def is_injection_attempt(text: str) -> bool:
    """Heuristic check for common prompt injection patterns."""
    return any(p.search(text) for p in _COMPILED_PATTERNS)
