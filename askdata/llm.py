"""Minimal Gemini REST client (free tier) with JSON output, token accounting and polite backoff.

No SDK: one POST to generativelanguage.googleapis.com. The key is read from GEMINI_API_KEY and is
passed as the `key` query parameter, never logged.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .prompt import SYSTEM

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"sql": {"type": "STRING"}, "refuse": {"type": "BOOLEAN"}, "reason": {"type": "STRING"}},
    "required": ["sql", "refuse", "reason"],
}


@dataclass
class LLMReply:
    sql: str
    refuse: bool
    reason: str
    raw: str
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    latency_ms: float
    parse_error: str | None = None


def model_config(model: str) -> dict:
    """Gemma models on the Gemini API take no system instruction and no JSON mode."""
    gemma = model.startswith("gemma")
    return {"system_instruction": not gemma, "json_mode": not gemma,
            "thinking_off": model.startswith("gemini-2.5-flash") and "lite" not in model}


def request_body(model: str, user_text: str) -> dict:
    cfg = model_config(model)
    gen: dict = {"temperature": 0, "maxOutputTokens": 2048}
    if cfg["json_mode"]:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = RESPONSE_SCHEMA
    if cfg["thinking_off"]:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body: dict = {"generationConfig": gen}
    if cfg["system_instruction"]:
        body["systemInstruction"] = {"parts": [{"text": SYSTEM}]}
        body["contents"] = [{"role": "user", "parts": [{"text": user_text}]}]
    else:
        body["contents"] = [{"role": "user", "parts": [{"text": SYSTEM + "\n\n" + user_text}]}]
    return body


def parse_reply(text: str) -> tuple[str, bool, str, str | None]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        m = re.search(r"(?is)\b(select|with)\b.*", t)
        return (m.group(0).strip() if m else "", False, "", "no JSON object in reply")
    try:
        obj = json.loads(t[a:b + 1])
    except json.JSONDecodeError as e:
        return "", False, "", f"invalid JSON: {e}"
    sql = (obj.get("sql") or "").strip()
    refuse = bool(obj.get("refuse")) or sql.startswith("-- refuse")
    if sql.startswith("--"):
        sql = ""
    return sql, refuse, str(obj.get("reason") or ""), None


class RateLimited(Exception):
    pass


def generate(model: str, user_text: str, *, key: str | None = None, max_wait_s: float = 600) -> LLMReply:
    key = key or os.environ["GEMINI_API_KEY"]
    data = json.dumps(request_body(model, user_text)).encode()
    waited, delay = 0.0, 5.0
    while True:
        req = urllib.request.Request(API.format(model=model) + "?key=" + key, data=data,
                                     headers={"Content-Type": "application/json", "User-Agent": "askdata-eval/0.1"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in (429, 500, 503) and waited < max_wait_s:
                m = re.search(r'"retryDelay":\s*"(\d+)s"', body)
                if "PerDay" in body or "per day" in body.lower():
                    raise RateLimited(f"{model}: daily quota exhausted") from None
                sleep = float(m.group(1)) + 1 if m else delay
                time.sleep(sleep)
                waited += sleep
                delay = min(delay * 2, 60)
                continue
            raise RuntimeError(f"{model} HTTP {e.code}: {body[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if waited < max_wait_s:
                time.sleep(delay)
                waited += delay
                continue
            raise RuntimeError(f"{model}: {e}") from None
    latency = (time.perf_counter() - t0) * 1000
    cand = (d.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    u = d.get("usageMetadata", {})
    sql, refuse, reason, perr = parse_reply(text)
    return LLMReply(sql, refuse, reason, text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0),
                    u.get("thoughtsTokenCount", 0), latency, perr)


def answer_prompt(question: str, sql: str, columns: list, rows: list, total: int) -> str:
    """Mirror of the `mode: "answer"` prompt in web/functions/api/ask.js."""
    cols = [str(c) for c in columns[:12]]
    body = [[str(v)[:80] for v in r[:12]] for r in rows[:20]]
    table = "\n".join([" | ".join(cols)] + [" | ".join(r) for r in body])
    return (f"Question: {question}\nSQL: {sql[:3000]}\nResult ({len(rows[:20])} rows shown of {total}):\n{table}\n\n"
            "Answer the question in one or two plain sentences using only numbers and names from the result. "
            "Pay values are annual USD. If the result is a long list, summarise the top of it. Do not mention SQL.")


def generate_text(model: str, prompt: str, key: str | None = None) -> str:
    key = key or os.environ["GEMINI_API_KEY"]
    gen: dict = {"temperature": 0, "maxOutputTokens": 200}
    if model_config(model)["thinking_off"]:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen}
    req = urllib.request.Request(API.format(model=model) + "?key=" + key, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "askdata-eval/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
