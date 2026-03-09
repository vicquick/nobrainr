"""Distill — compress text via local LLM before sending to expensive agent.

Inspired by samuelfaj/distill. Uses the local Ollama scheduler LLM to extract
only the essential information from raw text, reducing token cost by 90-99%.
"""

import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# Use a small, fast model for distillation (scheduler LLM is fine)
DISTILL_MODEL = None  # Will use settings.scheduler_llm_model


def _build_prompt(question: str, text: str) -> str:
    """Build the distillation prompt."""
    return "\n".join([
        "You compress text for another paid language model.",
        "Rules:",
        "- Answer only what the question asks.",
        "- No markdown formatting.",
        "- Keep the answer extremely short but complete.",
        "- Prefer one sentence. Never exceed five short lines.",
        "- Never ask for more input.",
        "- If the text is insufficient, reply: 'Insufficient information.'",
        "- If the source is already shorter than your answer would be, reuse the source wording.",
        "- Preserve exact names, IDs, paths, URLs, and numbers — never paraphrase identifiers.",
        "",
        f"Question: {question}",
        "",
        "Text to distill:",
        text,
    ])


async def distill_text(
    text: str,
    question: str,
    *,
    max_input_chars: int = 50000,
    model: str | None = None,
) -> dict:
    """Compress text using the local LLM, extracting only what's relevant.

    Args:
        text: Raw text to compress.
        question: What information to extract (e.g. "what errors occurred?").
        max_input_chars: Truncate input beyond this length.
        model: Override LLM model (defaults to scheduler_llm_model).

    Returns:
        {"distilled": str, "input_chars": int, "output_chars": int, "compression_ratio": float}
    """
    if not text or not text.strip():
        return {"distilled": "", "input_chars": 0, "output_chars": 0, "compression_ratio": 0.0}

    text = text[:max_input_chars]
    input_chars = len(text)

    # If text is already very short, just return it
    if input_chars < 200:
        return {
            "distilled": text.strip(),
            "input_chars": input_chars,
            "output_chars": len(text.strip()),
            "compression_ratio": 1.0,
        }

    prompt = _build_prompt(question, text)
    use_model = model or DISTILL_MODEL or settings.scheduler_llm_model

    try:
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=60.0) as client:
            resp = await client.post("/api/generate", json={
                "model": use_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 200,
                    "num_ctx": 4096,
                },
                "keep_alive": "5m",
            })
            resp.raise_for_status()
            data = resp.json()
            distilled = data.get("response", "").strip()

            if not distilled:
                return {"distilled": text[:500], "input_chars": input_chars,
                        "output_chars": min(500, input_chars), "compression_ratio": 1.0,
                        "fallback": True}

            output_chars = len(distilled)
            ratio = output_chars / input_chars if input_chars > 0 else 0.0

            return {
                "distilled": distilled,
                "input_chars": input_chars,
                "output_chars": output_chars,
                "compression_ratio": round(ratio, 4),
                "tokens_saved_estimate": max(0, (input_chars - output_chars) // 4),
            }

    except Exception as e:
        logger.warning("Distill failed, returning truncated original: %s", e)
        return {
            "distilled": text[:500],
            "input_chars": input_chars,
            "output_chars": min(500, input_chars),
            "compression_ratio": 1.0,
            "error": str(e),
        }


async def distill_memories(
    memories: list[dict],
    question: str,
) -> dict:
    """Distill a list of memory search results into a compressed answer.

    Takes the full search results and compresses them into a focused answer
    to the question, dramatically reducing tokens sent to the agent.

    Args:
        memories: List of memory dicts from search results.
        question: What to extract from the memories.

    Returns:
        {"distilled": str, "memory_count": int, "input_chars": int, ...}
    """
    if not memories:
        return {"distilled": "No memories found.", "memory_count": 0,
                "input_chars": 0, "output_chars": 0, "compression_ratio": 0.0}

    # Build a compact text representation of the memories
    parts = []
    for i, mem in enumerate(memories, 1):
        content = mem.get("content", "")
        summary = mem.get("summary", "")
        tags = mem.get("tags", [])
        category = mem.get("category", "")
        header = f"[{i}] {category}"
        if tags:
            header += f" ({', '.join(tags[:5])})"
        if summary:
            header += f" — {summary}"
        parts.append(f"{header}\n{content}")

    combined = "\n---\n".join(parts)

    result = await distill_text(combined, question)
    result["memory_count"] = len(memories)
    return result
