// Emits prompts and guard verdicts from the JS core for tests/test_parity.py to compare with Python.
import { readFileSync } from "node:fs";
import { buildPrompt, checkSql, parseReply, systemPrompt } from "../web/public/js/core.mjs";
const A = JSON.parse(readFileSync(new URL("../web/shared/assets.json", import.meta.url)));
const fx = JSON.parse(readFileSync(0, "utf8"));
const prompts = [];
for (const q of fx.questions) for (const ab of ["a", "b", "c", "d", "e"]) prompts.push(buildPrompt(q, ab, "de".includes(ab) ? fx.attempts : [], A));
const systems = ["a", "b", "c", "d", "e"].map((ab) => systemPrompt(ab, A));
const verdicts = fx.sql.map((s) => { const v = checkSql(s, A.guard); return [v.ok, v.reason]; });
const replies = fx.replies.map((r) => { const p = parseReply(r); return [p.sql, p.refuse, p.reason]; });
process.stdout.write(JSON.stringify({ prompts, verdicts, replies, systems }));
