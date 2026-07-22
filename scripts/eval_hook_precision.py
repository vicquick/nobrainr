"""M2: hook injection-precision eval (HEART PLAN measure phase, 2026-07-22).

Answers: of the memories the UserPromptSubmit hook injects, how many are
actually relevant to the prompt? This is THE number behind "agents don't
know enough" — a noisy hook buries the good context.

Two modes:
  --baseline   mine existing Claude Code transcripts (the v2 raw-vector era
               plus early v3): extract (prompt, injected-memory) pairs from
               <prompt-context> blocks and LLM-judge relevance on the CPU
               tier (qwen3-8b-cpu via llama-swap — zero GPU contention).
  --v3-log     judge pairs from ~/.claude/logs/hook-injections.jsonl
               (hook v3 writes it; needs memory summaries fetched by id).

Output: precision@k = judged-relevant / judged, plus per-prompt breakdown.
Stdlib only — runs on the HOST (transcripts live there), no ML deps.

Usage:
    python3 scripts/eval_hook_precision.py --baseline \
        --transcripts ~/.claude/projects/-root --sample 40
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import urllib.request

LLM_URL = os.environ.get("NOBRAINR_JUDGE_URL", "http://10.10.10.12:8080/v1/chat/completions")
LLM_MODEL = os.environ.get("NOBRAINR_JUDGE_MODEL", "qwen3-8b-cpu")

_JUDGE_SYSTEM = (
    "You judge whether a memory snippet would genuinely help answer a user "
    "prompt. Reply with EXACTLY one word: YES if the snippet is topically "
    "relevant and could inform the answer, NO if it is unrelated filler "
    "(different project, generic status pulse, unrelated commit, stale "
    "boilerplate). Be strict: tangential same-server trivia is NO."
)


def judge(prompt: str, snippet: str) -> bool | None:
    body = json.dumps({
        "model": LLM_MODEL,
        "temperature": 0.0,
        "max_tokens": 4,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": f"PROMPT: {prompt[:400]}\n\nMEMORY SNIPPET: {snippet[:400]}\n\nRelevant?"},
        ],
    }).encode()
    req = urllib.request.Request(
        LLM_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)["choices"][0]["message"]["content"].strip().upper()
        if "YES" in out:
            return True
        if "NO" in out:
            return False
        return None
    except Exception:
        return None


_MEM_LINE = re.compile(r"^- \[[\w-]+\] (.{20,300})", re.M)


def mine_transcripts(root: str, sample: int) -> list[tuple[str, str]]:
    """Extract (user_prompt, injected_memory_line) pairs, newest first.

    Transcript shape (verified 2026-07-22): the typed prompt is a
    `type=user` record with a plain-string message.content; the hook
    output follows as `type=attachment` records with
    attachment.hookName == "UserPromptSubmit" and the <prompt-context>
    block in attachment.content. Pair each attachment with the most
    recent preceding user prompt.
    """
    pairs: list[tuple[str, str]] = []
    files = sorted(glob.glob(os.path.join(root, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    for f in files[:6]:
        try:
            lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        file_pairs: list[tuple[str, str]] = []
        last_prompt = ""
        for line in lines:
            if '"type":"user"' not in line and '"type": "user"' not in line \
                    and '"hookName"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "user":
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, str) and len(c.strip()) >= 10 \
                        and "<system-reminder>" not in c:
                    last_prompt = c.strip()
                continue
            att = rec.get("attachment") or {}
            if att.get("hookName") != "UserPromptSubmit" or not last_prompt:
                continue
            content = att.get("content") or ""
            if "Relevant memories" not in content:
                continue
            for m in _MEM_LINE.finditer(content):
                file_pairs.append((last_prompt[:400], m.group(1)))
        # newest turns last in file → take from the end
        pairs.extend(reversed(file_pairs))
        if len(pairs) >= sample:
            return pairs[:sample]
    return pairs[:sample]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects/-root"))
    ap.add_argument("--sample", type=int, default=40)
    args = ap.parse_args()

    pairs = mine_transcripts(args.transcripts, args.sample)
    if not pairs:
        print(json.dumps({"error": "no pairs mined"}))
        return

    relevant = irrelevant = skipped = 0
    for i, (prompt, snippet) in enumerate(pairs):
        v = judge(prompt, snippet)
        if v is True:
            relevant += 1
        elif v is False:
            irrelevant += 1
        else:
            skipped += 1
        print(f"  [{i+1}/{len(pairs)}] {'REL' if v else ('IRR' if v is False else 'skip')} "
              f"| {snippet[:60]}", flush=True)

    judged = relevant + irrelevant
    print(json.dumps({
        "pairs_mined": len(pairs), "judged": judged,
        "relevant": relevant, "irrelevant": irrelevant, "skipped": skipped,
        "injection_precision": round(relevant / judged, 3) if judged else None,
    }, indent=1))


if __name__ == "__main__":
    main()
