import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.33.1-dev57.0/+esm";
import { checkSql } from "./core.mjs";

const TABLES = ["fct_posting", "dim_company", "posting_skills", "dim_skill", "posting_versions_scd2", "mart_daily_market",
  "mart_skill_demand", "mart_comp_by_family", "mart_degree_requirements", "mart_doorway_roles", "mart_crawl_coverage"];
const MAX_RETRIES = 2, ROW_LIMIT = 1000, TIMEOUT_MS = 10000;
const $ = (id) => document.getElementById(id);
let db, conn, guard, examples = [], ready = false, busy = false;

function status(msg, kind = "ok") { $("status").innerHTML = `<span class="dot ${kind}"></span>${esc(msg)}`; }
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function initDb() {
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const url = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
  db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(url));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(url);
  await db.open({ query: { castBigIntToDouble: true, castDecimalToDouble: true, castTimestampToDate: true } });
  conn = await db.connect();
  await Promise.all(TABLES.map((t) => db.registerFileURL(`${t}.parquet`, new URL(`/data/${t}.parquet`, location.href).href,
    duckdb.DuckDBDataProtocol.HTTP, false)));
  for (const t of TABLES) await conn.query(`CREATE TABLE ${t} AS SELECT * FROM read_parquet('${t}.parquet')`);
  // Same session as the Python eval: ICU loaded (timestamptz functions such as strftime/dayname on
  // timestamptz, time zones), UTC for date logic, and only then no file/network access at all. ICU has to be
  // loaded before external access is disabled, or its lazy autoload fails later with a binder error.
  try { await conn.query("LOAD icu"); } catch (e) { console.warn("icu", e); }
  try { await conn.query("SET TimeZone = 'UTC'"); } catch (e) { console.warn("TimeZone", e); }
  window.__askdataSession = (await conn.query("SELECT current_setting('TimeZone') AS tz, (SELECT count(*) FROM duckdb_extensions() WHERE extension_name = 'icu' AND loaded) AS icu")).toArray()[0].toJSON();
  await conn.query("SET enable_external_access = false");
  try { await conn.query("SET lock_configuration = true"); } catch (e) { console.warn("lock", e); }
}

function cell(v, typeId) {
  if (v === null || v === undefined) return null;
  if (typeof v === "bigint") return Number(v);
  if (v instanceof Date) v = v.getTime();
  if ((typeId === 8 || typeId === 10) && typeof v === "number") {
    const iso = new Date(v).toISOString();
    return iso.endsWith("T00:00:00.000Z") ? iso.slice(0, 10) : iso.slice(0, 19).replace("T", " ");
  }
  if (typeof v === "object") return v.toString ? v.toString() : JSON.stringify(v);
  return v;
}

async function execute(sql) {
  const v = checkSql(sql, guard);
  if (!v.ok) return { error: `Blocked by read-only guard: ${v.reason}` };
  const t0 = performance.now();
  let timer;
  try {
    const res = await Promise.race([
      conn.query(v.sql),
      new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`Query timed out after ${TIMEOUT_MS / 1000}s`)), TIMEOUT_MS); }),
    ]);
    const fields = res.schema.fields;
    const n = Math.min(res.numRows, ROW_LIMIT);
    const cols = fields.map((f, i) => ({ name: f.name, typeId: f.type.typeId, vec: res.getChildAt(i) }));
    const rows = [];
    for (let r = 0; r < n; r++) rows.push(cols.map((c) => cell(c.vec.get(r), c.typeId)));
    return { columns: fields.map((f) => f.name), types: cols.map((c) => c.typeId), rows, total: res.numRows, ms: performance.now() - t0 };
  } catch (e) {
    if (String(e.message).includes("timed out")) { ready = false; reboot(); }
    return { error: String(e.message || e).split("\n")[0].slice(0, 600) };
  } finally { clearTimeout(timer); }
}

async function reboot() {
  status("Restarting the in-browser database after a timeout...", "busy");
  try { await db.terminate(); } catch {}
  await initDb();
  ready = true; status("Ready.");
}

async function api(body) {
  const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (r.status === 429 || d.limited) return { limited: true, message: d.message || "The free model quota is used up." };
  if (!r.ok) return { error: d.error || `HTTP ${r.status}` };
  return d;
}

function fmt(v) {
  if (v === null) return '<span style="color:var(--ink-3)">null</span>';
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  if (typeof v === "string" && /^https?:\/\//.test(v)) return `<a href="${esc(v)}" rel="nofollow noopener" target="_blank">${esc(v)}</a>`;
  return esc(v);
}

function renderTable(res) {
  const num = res.rows.length ? res.columns.map((_, j) => res.rows.every((r) => r[j] === null || typeof r[j] === "number")) : [];
  $("rowcount").textContent = `${res.total.toLocaleString()} row${res.total === 1 ? "" : "s"}${res.total > res.rows.length ? `, first ${res.rows.length} shown` : ""}`;
  $("table").innerHTML = `<table><thead><tr>${res.columns.map((c, j) => `<th class="${num[j] ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead><tbody>` +
    res.rows.slice(0, 300).map((r) => `<tr>${r.map((v, j) => `<td class="${num[j] ? "num" : ""}">${fmt(v)}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
}

function renderChart(res) {
  const el = $("chart");
  const n = res.rows.length;
  const isNum = (j) => res.rows.every((r) => r[j] === null || typeof r[j] === "number");
  const labelJ = res.columns.findIndex((_, j) => !isNum(j));
  const valueJ = res.columns.findIndex((_, j) => j !== labelJ && isNum(j));
  if (n < 2 || n > 40 || labelJ < 0 || valueJ < 0) {
    el.innerHTML = `<p class="meta">No chart for this shape of result (a chart needs 2-40 rows with a label and a number).</p>`;
    return;
  }
  let rows = res.rows.map((r) => ({ label: String(r[labelJ]), value: r[valueJ] ?? 0 }));
  const dated = rows.every((r) => /^\d{4}-\d{2}-\d{2}/.test(r.label));
  if (dated) rows.sort((a, b) => a.label.localeCompare(b.label));
  const max = Math.max(...rows.map((r) => r.value), 0) || 1;
  const W = 460, labelW = 150, rowH = 24, H = rows.length * rowH + 8;
  const bars = rows.map((r, i) => {
    const w = Math.max(2, (W - labelW - 60) * Math.max(0, r.value) / max);
    const y = i * rowH + 4;
    const lab = r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label;
    return `<text x="${labelW - 8}" y="${y + 15}" text-anchor="end">${esc(lab)}</text>` +
      `<rect class="bar" x="${labelW}" y="${y + 3}" width="${w}" height="${rowH - 8}" rx="3" data-tip="${esc(r.label)}: ${esc(fmtPlain(r.value))}"></rect>` +
      `<text x="${labelW + w + 6}" y="${y + 15}">${esc(fmtPlain(r.value))}</text>`;
  }).join("");
  el.innerHTML = `<p class="meta" style="margin:0 0 6px">${esc(res.columns[valueJ])} by ${esc(res.columns[labelJ])}</p>` +
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Bar chart of ${esc(res.columns[valueJ])} by ${esc(res.columns[labelJ])}">${bars}</svg>`;
}

function fmtPlain(v) {
  if (typeof v !== "number") return String(v);
  if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString("en-US");
  return Number.isInteger(v) ? String(v) : v.toLocaleString("en-US", { maximumFractionDigits: 3 });
}

function renderAttempts(attempts) {
  $("attempts").innerHTML = attempts.map((a, i) => `<li class="${a.error ? "fail" : ""}">
    <strong>Attempt ${i + 1}</strong>${a.error ? " failed" : a.refused ? " refused" : " succeeded"}${a.ms != null ? ` <span class="meta">(${Math.round(a.ms)} ms model${a.execMs != null ? `, ${Math.round(a.execMs)} ms query` : ""})</span>` : ""}
    ${a.sql ? `<pre class="sql">${esc(a.sql)}</pre>` : ""}${a.error ? `<div class="err">${esc(a.error)}</div>` : ""}
    ${a.refused ? `<div class="meta">Refused: ${esc(a.reason || "")}</div>` : ""}</li>`).join("");
}

function show(state) {
  $("out").hidden = false;
  $("cachedTag").hidden = !state.cached;
  $("answer").textContent = state.answer || "";
  $("meta").textContent = state.meta || "";
  renderAttempts(state.attempts);
  if (state.result && !state.result.error) { renderTable(state.result); renderChart(state.result); }
  else { $("table").innerHTML = '<p class="meta" style="padding:10px">No result.</p>'; $("rowcount").textContent = ""; $("chart").innerHTML = ""; }
  $("copy").onclick = () => navigator.clipboard?.writeText(state.attempts.filter((a) => a.sql).at(-1)?.sql || "");
}

async function runCached(ex) {
  const res = await execute(ex.sql);
  show({ cached: true, answer: ex.answer, meta: `Cached answer from the build (${ex.model}); the SQL was re-run just now in your browser.`,
    attempts: ex.attempts.map((a) => ({ ...a, ms: null })), result: res });
}

async function askLive(question) {
  $("limited").hidden = true;
  const attempts = [];
  let result = null, total = 0, cfg = "";
  for (let n = 0; n <= MAX_RETRIES; n++) {
    status(n ? `Self-correcting (retry ${n} of ${MAX_RETRIES})...` : "Writing SQL...", "busy");
    const r = await api({ question, attempts: attempts.filter((a) => a.error).map((a) => ({ sql: a.sql || "(none)", error: a.error })) });
    if (r.limited) return limitedMode(r.message, question);
    if (r.error) { status(`Error: ${r.error}`, ""); return; }
    total += r.latency_ms || 0;
    cfg = `${r.model}, ablation ${r.ablation}`;
    if (r.refuse) {
      attempts.push({ refused: true, reason: r.reason, ms: r.latency_ms });
      show({ answer: `I can't answer that from this warehouse. ${r.reason || ""}`.trim(), meta: `${cfg} · ${Math.round(total)} ms`, attempts, result: null });
      status("Ready.");
      return;
    }
    status("Running the query in your browser...", "busy");
    const res = r.sql ? await execute(r.sql) : { error: r.parse_error || "The model returned no SQL." };
    const err = res.error || (res.rows.length ? null : "The query ran but returned 0 rows. Check filter values against the listed sample values.");
    attempts.push({ sql: r.sql, error: err, ms: r.latency_ms, execMs: res.ms });
    result = res;
    if (!err) break;
  }
  let answer = "", meta = "";
  if (result && !result.error && result.rows.length) {
    status("Summarising...", "busy");
    const last = attempts.at(-1);
    const a = await api({ mode: "answer", question, sql: last.sql, columns: result.columns, rows: result.rows.slice(0, 20), total: result.total });
    answer = a.answer || (a.limited ? "(Summary skipped: the model quota is used up. The result table below is complete.)" : "");
    meta = `${cfg} · ${attempts.length} attempt${attempts.length > 1 ? "s" : ""} · ${Math.round(total + (a.latency_ms || 0))} ms of model time`;
  } else {
    answer = "No answer: every attempt failed. The attempts below show the SQL and the errors.";
  }
  show({ answer, meta, attempts, result });
  status("Ready.");
}

function limitedMode(message, question) {
  const n = $("limited");
  n.hidden = false;
  n.innerHTML = `<strong>${esc(message)}</strong> The live model runs on a free quota. Try one of the example questions above: their SQL was generated at build time and runs live in your browser.`;
  status("Live questions paused.", "");
}

async function main() {
  try {
    [guard, examples] = await Promise.all([fetch("/data/guard.json").then((r) => r.json()), fetch("/data/examples.json").then((r) => r.json())]);
  } catch { examples = examples || []; }
  $("chips").innerHTML = examples.map((e, i) => `<button type="button" class="chip" data-i="${i}">${esc(e.question)}</button>`).join("");
  $("chips").onclick = async (ev) => {
    const b = ev.target.closest(".chip");
    if (!b || !ready || busy) return;
    const ex = examples[Number(b.dataset.i)];
    $("q").value = ex.question;
    busy = true; try { await runCached(ex); } finally { busy = false; }
  };
  $("ask").onsubmit = async (ev) => {
    ev.preventDefault();
    const q = $("q").value.trim();
    if (!q || !ready || busy) return;
    const ex = examples.find((e) => e.question.toLowerCase() === q.toLowerCase());
    busy = true; $("go").disabled = true;
    try { ex ? await runCached(ex) : await askLive(q); } catch (e) { status(`Error: ${e.message || e}`, ""); }
    finally { busy = false; $("go").disabled = false; }
  };
  document.addEventListener("mousemove", (e) => {
    const t = e.target.closest?.("[data-tip]"), tip = $("tip");
    if (!t) { tip.style.opacity = 0; return; }
    tip.textContent = t.dataset.tip; tip.style.opacity = 1;
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 290) + "px"; tip.style.top = e.clientY + 12 + "px";
  });
  try {
    await initDb();
    ready = true; $("go").disabled = false;
    status("Ready. 11 tables loaded in your browser.");
  } catch (e) {
    status(`Could not start DuckDB-WASM: ${e.message || e}`, "");
  }
}
main();
