// POST /api/ask — the only server-side piece of askdata. It builds the grounded prompt (same code and
// data as the Python eval), calls Gemini with a key held as a Pages secret, and returns the SQL. The
// SQL itself runs in the visitor's browser on DuckDB-WASM, so this function never touches data.
//
// Body: { question, attempts?: [{sql, error}] }            -> { sql, refuse, reason, model, tokens, latency_ms }
//       { mode: "answer", question, sql, columns, rows }     -> { answer }
// Limits: per-IP daily calls, a global daily cap (both in KV), and Gemini's own quota. Any of them
// returns 429 { limited: true } and the page falls back to cached example answers.
import A from "../../shared/assets.json";
import { buildPrompt, parseReply, systemPrompt } from "../../public/js/core.mjs";

// Primary and fallback configurations, both measured on /accuracy (see wrangler.toml [vars]).
const primary = (env) => ({ model: env.MODEL || "gemini-2.5-flash", ablation: env.ABLATION || "d" });
const fallback = (env) => ({ model: env.FALLBACK_MODEL || "gemini-2.5-flash-lite", ablation: env.FALLBACK_ABLATION || "e" });

const PER_IP_DAILY = 40;
const GLOBAL_DAILY = 300;
const burst = new Map(); // best-effort per-isolate burst limiter: ip -> [timestamps]

const RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: { sql: { type: "STRING" }, refuse: { type: "BOOLEAN" }, reason: { type: "STRING" } },
  required: ["sql", "refuse", "reason"],
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

const limited = (message) => json({ limited: true, message }, 429);

function sameOrigin(request) {
  const o = request.headers.get("Origin");
  if (!o) return true;
  try {
    const h = new URL(o).hostname;
    return h === new URL(request.url).hostname;
  } catch { return false; }
}

async function spend(env, ip) {
  const now = Date.now();
  const recent = (burst.get(ip) || []).filter((t) => now - t < 60_000);
  if (recent.length >= 8) return "Too many requests in the last minute.";
  recent.push(now);
  burst.set(ip, recent);
  if (!env.RL) return null;
  const day = new Date().toISOString().slice(0, 10);
  const ipKey = `ip:${day}:${ip}`, gKey = `g:${day}`;
  const [ipN, gN] = await Promise.all([env.RL.get(ipKey), env.RL.get(gKey)]);
  if (Number(gN || 0) >= GLOBAL_DAILY) return "The site's daily question budget is used up.";
  if (Number(ipN || 0) >= PER_IP_DAILY) return "You have reached today's question limit.";
  try {
    await Promise.all([
      env.RL.put(ipKey, String(Number(ipN || 0) + 1), { expirationTtl: 172800 }),
      env.RL.put(gKey, String(Number(gN || 0) + 1), { expirationTtl: 172800 }),
    ]);
  } catch { return "The site's daily question budget is used up."; }
  return null;
}

function requestFor(model, systemText, userText, gen) {
  if (model.startsWith("gemma")) { // no system instruction and no JSON mode for Gemma on this API
    return { contents: [{ role: "user", parts: [{ text: systemText + "\n\n" + userText }] }], generationConfig: { temperature: 0, maxOutputTokens: gen.maxOutputTokens } };
  }
  return { systemInstruction: { parts: [{ text: systemText }] }, contents: [{ role: "user", parts: [{ text: userText }] }], generationConfig: gen };
}

async function gemini(env, model, body) {
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${env.GEMINI_API_KEY}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (r.status === 429) return { limited: true };
  if (!r.ok) return { error: `model error ${r.status}` };
  const d = await r.json();
  const parts = d.candidates?.[0]?.content?.parts || [];
  return { model, text: parts.filter((p) => !p.thought).map((p) => p.text || "").join(""), usage: d.usageMetadata || {} };
}

function thinkingOff(model) { return model.startsWith("gemini-2.5-flash") && !model.includes("lite"); }

export async function onRequestPost({ request, env }) {
  if (!sameOrigin(request)) return json({ error: "forbidden" }, 403);
  if (!env.GEMINI_API_KEY) return limited("The model is not configured.");
  let b;
  try { b = await request.json(); } catch { return json({ error: "bad json" }, 400); }
  const question = String(b.question || "").trim();
  if (!question || question.length > 300) return json({ error: "Ask a question of 1-300 characters." }, 400);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const why = await spend(env, ip);
  if (why) return limited(why);
  const t0 = Date.now();

  if (b.mode === "answer") {
    const cols = (b.columns || []).slice(0, 12).map(String);
    const rows = (b.rows || []).slice(0, 20).map((r) => r.slice(0, 12).map((v) => String(v).slice(0, 80)));
    const table = [cols.join(" | "), ...rows.map((r) => r.join(" | "))].join("\n");
    const prompt = `Question: ${question}\nSQL: ${String(b.sql || "").slice(0, 3000)}\nResult (${(b.rows || []).length} rows shown of ${Number(b.total) || (b.rows || []).length}):\n${table}\n\n` +
      "Answer the question in one or two plain sentences using only numbers and names from the result. " +
      "Pay values are annual USD. If the result is a long list, summarise the top of it. Do not mention SQL.";
    const model = env.ANSWER_MODEL || "gemini-2.5-flash-lite";
    const gen = { temperature: 0, maxOutputTokens: 200 };
    if (thinkingOff(model)) gen.thinkingConfig = { thinkingBudget: 0 };
    const g = await gemini(env, model, { contents: [{ role: "user", parts: [{ text: prompt }] }], generationConfig: gen });
    if (g.limited) return limited("The model's free quota is used up for now.");
    if (g.error) return json({ error: g.error }, 502);
    return json({ answer: g.text.trim(), latency_ms: Date.now() - t0 });
  }

  const attempts = (Array.isArray(b.attempts) ? b.attempts : []).slice(0, 2)
    .map((a) => ({ sql: String(a.sql || "(none)").slice(0, 4000), error: String(a.error || "").slice(0, 600) }));
  let cfg = primary(env), g;
  for (const c of [primary(env), fallback(env)]) {
    cfg = c;
    const gen = { temperature: 0, maxOutputTokens: 2048, responseMimeType: "application/json", responseSchema: RESPONSE_SCHEMA };
    if (thinkingOff(c.model)) gen.thinkingConfig = { thinkingBudget: 0 };
    g = await gemini(env, c.model, requestFor(c.model, systemPrompt(c.ablation, A), buildPrompt(question, c.ablation, attempts, A), gen));
    if (!g.limited) break;
  }
  if (g.limited) return limited("The model's free quota is used up for now.");
  if (g.error) return json({ error: g.error }, 502);
  const p = parseReply(g.text);
  return json({
    sql: p.sql, refuse: p.refuse && !p.sql, reason: p.reason, parse_error: p.parseError, model: g.model, ablation: cfg.ablation,
    tokens: { prompt: g.usage.promptTokenCount || 0, output: g.usage.candidatesTokenCount || 0 }, latency_ms: Date.now() - t0,
  });
}

export async function onRequestGet() {
  return json({ ok: true, usage: "POST {question, attempts?}" });
}
