"""The agent loop: question -> grounded prompt -> SQL -> guarded execution -> self-correction (ablation d)."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .db import QueryResult, run
from .llm import LLMReply, generate
from .prompt import SYSTEM, build_prompt, system_prompt

MAX_RETRIES = 2


@dataclass
class Attempt:
    sql: str
    error: str | None
    n_rows: int
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    latency_ms: float
    raw: str


@dataclass
class Trace:
    question: str
    ablation: str
    model: str
    refused: bool = False
    refuse_reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    result: QueryResult | None = None
    wall_ms: float = 0.0

    @property
    def final_sql(self) -> str:
        return self.attempts[-1].sql if self.attempts else ""

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)

    def tokens(self) -> dict:
        return {"prompt": sum(a.prompt_tokens for a in self.attempts),
                "output": sum(a.output_tokens for a in self.attempts),
                "thinking": sum(a.thinking_tokens for a in self.attempts)}

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("result")
        return d


def problem_with(res: QueryResult) -> str | None:
    if res.error:
        return res.error
    if not res.rows:
        return "The query ran but returned 0 rows. Check filter values against the listed sample values."
    return None


def ask(con, question: str, *, ablation: str = "d", model: str = "gemini-2.5-flash-lite",
        llm: Callable[..., LLMReply] | None = None, first_reply: LLMReply | None = None) -> Trace:
    """Run the agent. `first_reply` lets the eval reuse ablation c's first call for ablation d
    (identical prompt at temperature 0), so d only spends quota on its retries."""
    llm = llm or (lambda m, text, system=SYSTEM: generate(m, text, system=system))
    system = system_prompt(ablation)
    tr = Trace(question, ablation, model)
    t0 = time.perf_counter()
    history: list[dict] = []
    max_attempts = 1 + (MAX_RETRIES if ablation in ("d", "e") else 0)
    for n in range(max_attempts):
        prompt_ablation = "c" if ablation in ("d", "e") else ablation
        user = build_prompt(question, prompt_ablation, history)
        if first_reply is not None and n == 0:
            reply = first_reply
        elif system == SYSTEM:
            reply = llm(model, user)
        else:
            reply = llm(model, user, system=system)
        if reply.refuse and not reply.sql:
            tr.refused, tr.refuse_reason = True, reply.reason
            tr.attempts.append(Attempt("", None, 0, reply.prompt_tokens, reply.output_tokens,
                                       reply.thinking_tokens, reply.latency_ms, reply.raw))
            break
        sql = reply.sql
        res = run(con, sql) if sql else QueryResult(error=reply.parse_error or "The reply contained no SQL.")
        err = problem_with(res)
        tr.attempts.append(Attempt(sql, res.error or (None if res.rows else "empty result"), len(res.rows),
                                   reply.prompt_tokens, reply.output_tokens, reply.thinking_tokens,
                                   reply.latency_ms, reply.raw))
        tr.result = res
        if err is None:
            break
        history.append({"sql": sql or "(none)", "error": err})
    tr.wall_ms = (time.perf_counter() - t0) * 1000
    return tr
