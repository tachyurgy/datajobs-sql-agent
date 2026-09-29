// askdata core: prompt construction (BM25 retrieval over the data dictionary + few-shots) and the
// read-only SQL guard. A line-for-line mirror of askdata/prompt.py and askdata/guard.py; the data
// (system prompt, vocabulary, dictionary, few-shots, guard lists) comes from the same Python sources
// via askdata/export_web.py. tests/test_parity.py asserts byte-identical prompts and identical guard
// verdicts across both implementations.

// ------------------------------------------------------------------ retrieval + prompt
export function tokenize(text, A) {
  const stop = A._stop || (A._stop = new Set(A.stopwords));
  const out = [];
  for (let w of text.toLowerCase().split(/[^a-z0-9]+/)) {
    if (!w || stop.has(w)) continue;
    if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
    out.push(w);
  }
  return out;
}

export function expand(question, A) {
  const extra = [];
  for (const w of question.toLowerCase().split(/[^a-z0-9]+/)) {
    if (Object.prototype.hasOwnProperty.call(A.synonyms, w)) extra.push(...tokenize(A.synonyms[w], A));
  }
  return tokenize(question, A).concat(extra);
}

export class BM25 {
  constructor(docs, k1 = 1.2, b = 0.75) {
    this.docs = docs; this.k1 = k1; this.b = b; this.n = docs.length;
    let total = 0;
    for (const d of docs) total += d.length;
    this.avgdl = total / this.n;
    const df = new Map();
    for (const d of docs) for (const t of new Set(d)) df.set(t, (df.get(t) || 0) + 1);
    this.idf = new Map();
    for (const [t, c] of df) this.idf.set(t, Math.log(1 + (this.n - c + 0.5) / (c + 0.5)));
    this.tf = docs.map((d) => { const m = new Map(); for (const t of d) m.set(t, (m.get(t) || 0) + 1); return m; });
  }
  scores(query) {
    const q = [];
    for (const t of query) if (!q.includes(t)) q.push(t);
    return this.docs.map((d, i) => {
      let s = 0.0;
      const dl = d.length;
      for (const t of q) {
        const f = this.tf[i].get(t) || 0;
        if (f) s += this.idf.get(t) * f * (this.k1 + 1) / (f + this.k1 * (1 - this.b + this.b * dl / this.avgdl));
      }
      return s;
    });
  }
  ranked(query) {
    const sc = this.scores(query);
    return [...Array(this.n).keys()].sort((x, y) => (sc[y] - sc[x]) || (x - y)).filter((i) => sc[i] > 0);
  }
  top(query, k) { return this.ranked(query).slice(0, k); }
}

export function columnDocs(A) {
  const out = [];
  for (const t of A.dictionary.tables) {
    for (const c of t.columns) {
      let line = `${t.name}.${c.name} (${c.type})`;
      if (c.description) line += `: ${c.description}`;
      if (c.samples.length) {
        let s = c.samples.map((v) => JSON.stringify(v)).join(", ");
        if (s.length > A.sample_chars) {
          const cut = s.slice(0, A.sample_chars);
          const k = cut.lastIndexOf(", ");
          s = (k >= 0 ? cut.slice(0, k) : cut) + ", ...";
        }
        line += ` Values: ${s}`;
      }
      out.push([t.name, c.name, line]);
    }
  }
  return out;
}

function indexes(A) {
  if (!A._idx) {
    const docs = columnDocs(A);
    A._idx = {
      docs,
      cols: new BM25(docs.map(([t, c, line]) => tokenize(`${t} ${c} ${line}`, A))),
      ex: new BM25(A.fewshots.map((e) => tokenize(e.question, A))),
    };
  }
  return A._idx;
}

export function retrieveColumns(queryTokens, A) {
  const { docs, cols } = indexes(A);
  const seen = new Map();
  const out = [];
  for (const i of cols.ranked(queryTokens)) {
    const name = docs[i][1];
    if ((seen.get(name) || 0) >= A.max_same_column_name) continue;
    seen.set(name, (seen.get(name) || 0) + 1);
    out.push(docs[i][2]);
    if (out.length === A.top_k_columns) break;
  }
  return out;
}

export function schemaBlock(A, withDescriptions) {
  const lines = [];
  for (const t of A.dictionary.tables) {
    lines.push(`${t.name}(${t.columns.map((c) => `${c.name} ${c.type}`).join(", ")})`);
    if (withDescriptions && t.description) lines.push(`  -- ${t.description}`);
  }
  return lines.join("\n");
}

export function buildPrompt(question, ablation, attempts, A) {
  const q = expand(question, A);
  let parts = ["### Schema", schemaBlock(A, ablation !== "a")];
  if (["b", "c", "d"].includes(ablation)) {
    parts.push("", "### Notes", ...A.dictionary.notes.map((n) => `- ${n}`));
    parts.push("", "### Relevant columns", ...retrieveColumns(q, A).map((l) => `- ${l}`));
  }
  if (["c", "d"].includes(ablation)) {
    const top = indexes(A).ex.top(tokenize(question, A), A.top_k_examples);
    if (top.length) {
      parts.push("", "### Examples");
      for (const i of top) parts.push(`Q: ${A.fewshots[i].question}`, `SQL: ${A.fewshots[i].sql}`);
    }
  }
  (attempts || []).forEach((a, n) => {
    parts.push("", `### Attempt ${n + 1} failed`, `SQL: ${a.sql}`, `Problem: ${a.error}`,
      "Write a corrected query (or refuse if the question cannot be answered).");
  });
  parts.push("", "### Question", question.trim());
  return parts.join("\n");
}

export function parseReply(text) {
  let t = text.trim().replace(/^```(?:json)?\s*|\s*```$/g, "");
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a < 0 || b < a) {
    const m = t.match(/\b(select|with)\b[\s\S]*/i);
    return { sql: m ? m[0].trim() : "", refuse: false, reason: "", parseError: "no JSON object in reply" };
  }
  let obj;
  try { obj = JSON.parse(t.slice(a, b + 1)); } catch (e) { return { sql: "", refuse: false, reason: "", parseError: "invalid JSON" }; }
  let sql = String(obj.sql || "").trim();
  const refuse = Boolean(obj.refuse) || sql.startsWith("-- refuse");
  if (sql.startsWith("--")) sql = "";
  return { sql, refuse, reason: String(obj.reason || ""), parseError: null };
}

// ------------------------------------------------------------------ read-only guard
export function tokenizeSql(sql) {
  const toks = [];
  let i = 0;
  const n = sql.length;
  const isAlpha = (c) => /[A-Za-z_]/.test(c) || (c > "\x7f" && c.toLowerCase() !== c.toUpperCase());
  const isAlnum = (c) => /[A-Za-z0-9_]/.test(c) || (c > "\x7f" && /\p{L}|\p{N}/u.test(c));
  while (i < n) {
    const c = sql[i];
    if (/\s/.test(c)) { i++; }
    else if (sql.startsWith("--", i)) { const j = sql.indexOf("\n", i); i = j < 0 ? n : j + 1; }
    else if (sql.startsWith("/*", i)) {
      const j = sql.indexOf("*/", i + 2);
      if (j < 0) throw new Error("unterminated comment");
      i = j + 2;
    } else if (c === "'") {
      let j = i + 1;
      for (;;) {
        const k = sql.indexOf("'", j);
        if (k < 0) throw new Error("unterminated string literal");
        if (k + 1 < n && sql[k + 1] === "'") { j = k + 2; continue; }
        toks.push(["string", sql.slice(i + 1, k)]); i = k + 1; break;
      }
    } else if (c === '"') {
      const k = sql.indexOf('"', i + 1);
      if (k < 0) throw new Error("unterminated quoted identifier");
      toks.push(["qident", sql.slice(i + 1, k).toLowerCase()]); i = k + 1;
    } else if (isAlpha(c)) {
      let j = i + 1;
      while (j < n && (isAlnum(sql[j]) || sql[j] === "$")) j++;
      toks.push(["word", sql.slice(i, j).toLowerCase()]); i = j;
    } else if (/[0-9]/.test(c)) {
      let j = i + 1;
      while (j < n && (isAlnum(sql[j]) || sql[j] === ".")) j++;
      toks.push(["num", sql.slice(i, j)]); i = j;
    } else { toks.push(["sym", c]); i++; }
  }
  return toks;
}

const eq = (t, k, v) => t && t[0] === k && t[1] === v;

export function checkSql(sql, G) {
  if (!sql || !sql.trim()) return { ok: false, reason: "empty statement", sql: "" };
  sql = sql.trim().replace(/[\s;]+$/, "");
  let toks;
  try { toks = tokenizeSql(sql); } catch (e) { return { ok: false, reason: e.message, sql: "" }; }
  if (!toks.length) return { ok: false, reason: "empty statement", sql: "" };
  for (const [k, v] of toks) {
    if (k === "sym" && v === ";") return { ok: false, reason: "only a single statement is allowed", sql: "" };
    if (k === "sym" && v === "$") return { ok: false, reason: "dollar-quoted strings and parameters are not allowed", sql: "" };
  }
  const first = (toks.find(([k, v]) => !(k === "sym" && v === "(")) || ["", ""])[1];
  if (first !== "select" && first !== "with") return { ok: false, reason: "only SELECT or WITH queries are allowed", sql: "" };
  const known = new Set(G.known_tables), denyK = new Set(G.deny_keywords), denyF = new Set(G.deny_functions);
  const allowTF = new Set(G.allowed_table_functions), fromFn = new Set(G.from_in_function);
  const ctes = new Set();
  for (let i = 1; i < toks.length - 2; i++) {
    if (eq(toks[i], "word", "as") && eq(toks[i + 1], "sym", "(") && ["word", "qident"].includes(toks[i - 1][0])) ctes.add(toks[i - 1][1]);
  }
  const parens = [];
  for (let i = 0; i < toks.length; i++) {
    const [kind, val] = toks[i];
    if (kind === "sym" && val === "(") {
      const p = i > 0 ? toks[i - 1] : ["", ""];
      parens.push(p[0] === "word" ? p[1] : "");
      continue;
    }
    if (kind === "sym" && val === ")") { parens.pop(); continue; }
    if (kind !== "word") continue;
    const nxt = toks[i + 1] || ["", ""];
    const prev = i > 0 ? toks[i - 1] : ["", ""];
    if (denyK.has(val) && !eq(prev, "sym", ".")) return { ok: false, reason: `keyword '${val.toUpperCase()}' is not allowed in a read-only query`, sql: "" };
    if (eq(nxt, "sym", "(") && (denyF.has(val) || val.startsWith("read_") || val.endsWith("_scan"))) return { ok: false, reason: `function '${val}' is not allowed`, sql: "" };
    if (val === "from" || val === "join") {
      if (val === "from" && ((parens.length && fromFn.has(parens[parens.length - 1])) || eq(prev, "word", "distinct"))) continue;
      let j = i + 1;
      if (j >= toks.length) return { ok: false, reason: `dangling ${val.toUpperCase()}`, sql: "" };
      let [tk, tv] = toks[j];
      if (tk === "string") return { ok: false, reason: "reading files or URLs is not allowed", sql: "" };
      if (tk === "sym" && tv === "(") continue;
      if (tk === "word" && tv === "lateral") continue;
      if (tk === "word" || tk === "qident") {
        while (j + 2 < toks.length && eq(toks[j + 1], "sym", ".") && ["word", "qident"].includes(toks[j + 2][0])) { j += 2; tv = toks[j][1]; }
        const after = toks[j + 1] || ["", ""];
        if (eq(after, "sym", "(")) {
          if (!allowTF.has(tv)) return { ok: false, reason: `table function '${tv}' is not allowed`, sql: "" };
        } else if (!known.has(tv) && !ctes.has(tv)) return { ok: false, reason: `unknown table '${tv}'`, sql: "" };
      }
    }
  }
  return { ok: true, reason: "", sql };
}
