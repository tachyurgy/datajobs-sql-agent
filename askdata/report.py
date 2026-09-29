"""Render the eval results: web/public/accuracy.html (the "How accurate is this?" page) and results/RESULTS.md.

Every number on the page is read from results/eval*.json; nothing is typed by hand.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import RESULTS, ROOT
from .gold import load

OUT_HTML = ROOT / "web" / "public" / "accuracy.html"
OUT_MD = RESULTS / "RESULTS.md"
LIVE_MODEL_FILE = ROOT / "web" / "wrangler.toml"
MODEL_LABEL = {"gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite", "gemini-2.5-flash": "Gemini 2.5 Flash (thinking off)",
               "gemma-4-31b-it": "Gemma 4 31B (open weights)"}
SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"]
CAT_ORDER = ["lookup", "aggregation", "join", "window", "date", "ambiguous", "unanswerable"]


def e(s) -> str:
    return html.escape(str(s))


def pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def ci(pair) -> str:
    return f"{100 * pair[0]:.0f}-{100 * pair[1]:.0f}%"


def load_results() -> dict:
    merged = {"models": {}}
    for f in sorted(RESULTS.glob("eval*.json")):
        d = json.loads(f.read_text())
        for k in ("snapshot_date", "n_questions", "ablations"):
            merged.setdefault(k, d.get(k))
        merged["models"].update(d["models"])
    order = [m for m in MODEL_LABEL if m in merged["models"]] + [m for m in merged["models"] if m not in MODEL_LABEL]
    merged["models"] = {m: merged["models"][m] for m in order}
    return merged


def live_config() -> dict:
    """The [vars] the Pages Function runs with (MODEL, ABLATION, FALLBACK_MODEL, FALLBACK_ABLATION)."""
    out = {}
    for line in LIVE_MODEL_FILE.read_text().splitlines():
        if "=" in line and line.strip()[:1].isupper():
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def chart_svg(res: dict) -> str:
    models = list(res["models"])
    abl = ABL
    W, H, left, top, bottom = 720, 300, 44, 16, 52
    gw = (W - left - 10) / len(abl)
    bw = min(34, (gw - 24) / max(1, len(models)))
    parts = []
    for t in range(0, 101, 25):
        y = top + (H - top - bottom) * (1 - t / 100)
        parts.append(f'<line x1="{left}" x2="{W - 6}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)" stroke-width="1"/>'
                     f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{t}%</text>')
    for i, a in enumerate(abl):
        gx = left + i * gw + (gw - bw * len(models)) / 2
        parts.append(f'<text x="{left + i * gw + gw / 2:.1f}" y="{H - bottom + 20}" text-anchor="middle" font-weight="600">{a}</text>'
                     f'<text x="{left + i * gw + gw / 2:.1f}" y="{H - bottom + 36}" text-anchor="middle" class="sub">{e(short_label(a))}</text>')
        for j, m in enumerate(models):
            s = res["models"][m]["summary"][a]
            v, (lo, hi) = s["overall_accuracy"], s["overall_accuracy_ci95"]
            scale = lambda x: top + (H - top - bottom) * (1 - x)
            x = gx + j * bw
            y = scale(v)
            parts.append(f'<rect x="{x + 2:.1f}" y="{y:.1f}" width="{bw - 4:.1f}" height="{scale(0) - y:.1f}" rx="3" fill="{SERIES[j % 3]}" '
                         f'data-tip="{e(MODEL_LABEL.get(m, m))}, {a}: {100 * v:.0f}% (95% CI {ci((lo, hi))})"/>'
                         f'<line x1="{x + bw / 2:.1f}" x2="{x + bw / 2:.1f}" y1="{scale(hi):.1f}" y2="{scale(lo):.1f}" stroke="var(--ink)" stroke-width="1.5"/>'
                         f'<line x1="{x + bw / 2 - 4:.1f}" x2="{x + bw / 2 + 4:.1f}" y1="{scale(hi):.1f}" y2="{scale(hi):.1f}" stroke="var(--ink)" stroke-width="1.5"/>'
                         f'<line x1="{x + bw / 2 - 4:.1f}" x2="{x + bw / 2 + 4:.1f}" y1="{scale(lo):.1f}" y2="{scale(lo):.1f}" stroke="var(--ink)" stroke-width="1.5"/>')
    legend = "".join(f'<span><i style="background:{SERIES[j % 3]}"></i>{e(MODEL_LABEL.get(m, m))}</span>' for j, m in enumerate(models))
    return (f'<div class="legend">{legend}</div><svg viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="Overall accuracy by ablation and model, with 95% bootstrap intervals">{"".join(parts)}</svg>')


def short_label(a: str) -> str:
    return {"a": "schema only", "b": "+ column docs", "c": "+ few-shot", "d": "+ self-correct", "e": "+ refusal rule"}[a]


ABL = ["a", "b", "c", "d", "e"]
LIVE_ABLATION = live_config().get("ABLATION", "d")


def build() -> dict:
    res = load_results()
    gold = {g.id: g for g in load()}
    cfg = live_config()
    live = cfg.get("MODEL", "gemini-2.5-flash")
    fb, fb_ab = cfg.get("FALLBACK_MODEL"), cfg.get("FALLBACK_ABLATION")
    models = list(res["models"])
    ab_labels = res["ablations"]
    head = res["models"].get(live) or res["models"][models[0]]
    hs = head["summary"][LIVE_ABLATION]

    rows_html, md = [], []
    md.append("| Model | Ablation | Overall (60) | 95% CI | Exec. acc. (53 answerable) | strict | Refusal (7) | False refusals | Mean retries | Median latency | Prompt tokens |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for m in models:
        mm = res["models"][m]
        for a in ABL:
            s = mm["summary"][a]
            rows_html.append(
                f"<tr><td>{e(MODEL_LABEL.get(m, m))}</td><td><strong>{a}</strong> {e(ab_labels[a])}</td>"
                f"<td class=num><strong>{pct(s['overall_accuracy'])}</strong></td><td class=num>{ci(s['overall_accuracy_ci95'])}</td>"
                f"<td class=num>{pct(s['execution_accuracy'])}</td><td class=num>{pct(s['strict_execution_accuracy'])}</td>"
                f"<td class=num>{pct(s['refusal_accuracy'])}</td><td class=num>{s['false_refusals']}</td>"
                f"<td class=num>{s['mean_retries']:.2f}</td><td class=num>{s['latency_ms_median'] / 1000:.1f}s</td>"
                f"<td class=num>{s['prompt_tokens_mean']:,.0f}</td></tr>")
            md.append(f"| {MODEL_LABEL.get(m, m)} | {a}: {ab_labels[a]} | {pct(s['overall_accuracy'])} | {ci(s['overall_accuracy_ci95'])} | "
                      f"{pct(s['execution_accuracy'])} | {pct(s['strict_execution_accuracy'])} | {pct(s['refusal_accuracy'])} | "
                      f"{s['false_refusals']} | {s['mean_retries']:.2f} | {s['latency_ms_median'] / 1000:.1f}s | {s['prompt_tokens_mean']:,.0f} |")
        if mm["status"] != "complete":
            md.append(f"| {MODEL_LABEL.get(m, m)} | status: {mm['status']} (n={mm['n_questions']}) | | | | | | | | | |")

    delta_html, delta_md = [], ["| Model | Step | Change in overall accuracy | 95% CI |", "|---|---|---|---|"]
    for m in models:
        for a in ABL[1:]:
            d, lo, hi = res["models"][m]["summary"][a]["delta_vs_prev_overall"]
            prev = chr(ord(a) - 1)
            sig = "" if lo <= 0 <= hi else " *"
            delta_html.append(f"<tr><td>{e(MODEL_LABEL.get(m, m))}</td><td>{prev} &rarr; {a}</td><td class=num>{100 * d:+.1f} pts{sig}</td>"
                              f"<td class=num>{100 * lo:+.1f} to {100 * hi:+.1f}</td></tr>")
            delta_md.append(f"| {MODEL_LABEL.get(m, m)} | {prev} -> {a} | {100 * d:+.1f} pts{sig} | {100 * lo:+.1f} to {100 * hi:+.1f} |")

    cat_head = "".join(f"<th class=num>{e(c)}</th>" for c in CAT_ORDER)
    cat_rows = []
    for m in models:
        for a in ["a", LIVE_ABLATION]:
            bc = res["models"][m]["summary"][a]["by_category"]
            cat_rows.append(f"<tr><td>{e(MODEL_LABEL.get(m, m))}, {a}</td>" + "".join(
                f"<td class=num>{pct(bc[c]['accuracy'])} <span class=meta>({round(bc[c]['accuracy'] * bc[c]['n'])}/{bc[c]['n']})</span></td>"
                if c in bc else "<td></td>" for c in CAT_ORDER) + "</tr>")

    # taxonomy (ablation d), with examples from each model's rows
    classes: dict[str, dict[str, int]] = {}
    for m in models:
        for k, v in res["models"][m]["summary"][LIVE_ABLATION]["error_taxonomy"].items():
            classes.setdefault(k, {})[m] = v
    tax_rows = "".join(f"<tr><td>{e(k)}</td>" + "".join(f"<td class=num>{v.get(m, 0)}</td>" for m in models) + "</tr>"
                       for k, v in sorted(classes.items(), key=lambda kv: -sum(kv[1].values())))
    examples = []
    for k in sorted(classes, key=lambda k: -sum(classes[k].values())):
        shown = 0
        for m in models:
            for r in res["models"][m]["rows"][LIVE_ABLATION]:
                if r.get("error_class") == k and shown < 2:
                    g = gold[r["id"]]
                    examples.append(
                        f"<details><summary><span class=pill>{e(k)}</span> {e(r['id'])}: {e(g.question)} "
                        f"<span class=meta>({e(MODEL_LABEL.get(m, m))})</span></summary>"
                        f"<p class=meta>Model SQL{' (after ' + str(r['retries']) + ' retries)' if r['retries'] else ''}:</p>"
                        f"<pre class=sql>{e(r['sql'] or '(no SQL: refused - ' + (r.get('refuse_reason') or '') + ')')}</pre>"
                        + (f"<p class=meta>Error: {e(r['error'])}</p>" if r.get("error") and r["error"] != "empty result" else "")
                        + (f"<p class=meta>Gold SQL:</p><pre class=sql>{e(g.gold_sql)}</pre>" if g.gold_sql else
                           "<p class=meta>Gold: refuse (the warehouse cannot answer this).</p>")
                        + f"<p class=meta>Verdict: {e(r['verdict'])}</p></details>")
                    shown += 1
    # per-question grid
    q_rows = []
    for gid, g in gold.items():
        cells = ""
        for m in models:
            for a in ABL:
                r = next((x for x in res["models"][m]["rows"][a] if x["id"] == gid), None)
                cells += ("<td></td>" if r is None else
                          f"<td class=num><span class='pill {'y' if r['correct'] else 'n'}' title='{e(r['verdict'])}'>{'yes' if r['correct'] else 'no'}</span></td>")
        q_rows.append(f"<tr><td>{e(gid)}</td><td style='white-space:normal;min-width:260px'>{e(g.question)}</td><td>{e(g.category)}</td>{cells}</tr>")
    q_head = "".join(f"<th class=num>{e(MODEL_LABEL.get(m, m).split(' (')[0])} {a}</th>" for m in models for a in ABL)

    fallback_note = ""
    if fb and fb in res["models"] and fb_ab:
        fs = res["models"][fb]["summary"][fb_ab]
        fallback_note = (f", falling back to {e(MODEL_LABEL.get(fb, fb))} at ablation {fb_ab} "
                         f"({pct(fs['overall_accuracy'])} overall, 95% CI {ci(fs['overall_accuracy_ci95'])}) when its free quota runs out")
    g_summary = json.loads((RESULTS / "gold_results.json").read_text())
    statuses = "; ".join(f"{MODEL_LABEL.get(m, m)}: {res['models'][m]['status']} ({res['models'][m]['n_questions']} questions)" for m in models)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>askdata accuracy</title>
<meta name="description" content="Execution accuracy of the askdata text-to-SQL agent on a 60-question gold set, by model and ablation, with bootstrap confidence intervals and an error taxonomy.">
<link rel="canonical" href="https://askdata.levelbrook.com/accuracy">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/styles.css">
<style>:root{{--series-3:#1baf7a}} @media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{--series-3:#199e70}}}} :root[data-theme=dark]{{--series-3:#199e70}}
.chart text{{fill:var(--ink-2);font:12px var(--sans)}} .chart text.sub{{font-size:11px;fill:var(--ink-3)}} .wide{{overflow-x:auto}} .wide table td,.wide table th{{font-size:13px}}</style>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%231f5fbf'/%3E%3Cpath d='M6 16V9m4 7V6m4 10v-5m4 5V8' stroke='white' stroke-width='2.2' stroke-linecap='round'/%3E%3C/svg%3E">
</head><body>
<header class="top"><div class="wrap"><a class="brand" href="/"><svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5" fill="var(--accent)"/><path d="M6 16V9m4 7V6m4 10v-5m4 5V8" stroke="var(--accent-ink)" stroke-width="2.2" stroke-linecap="round"/></svg>askdata</a>
<nav><a href="/">Ask</a><a href="/accuracy" aria-current="page">How accurate is this?</a><a href="https://github.com/tachyurgy/datajobs-sql-agent">Code</a></nav></div></header>
<main class="wrap">
<h1>How accurate is this?</h1>
<p class="lede">askdata is scored on a gold set of {g_summary['n_items']} questions ({g_summary['n_answerable']} answerable, {g_summary['n_items'] - g_summary['n_answerable']} that should be refused)
with hand-written, cross-checked gold SQL. A question counts as correct when the generated query's result matches the gold result, or when an unanswerable question is refused.
The live site runs <strong>{e(MODEL_LABEL.get(live, live))}</strong> at ablation {LIVE_ABLATION}{fallback_note}.</p>

<div class="kpis">
 <div class="card kpi"><div class="v">{pct(hs['overall_accuracy'])}</div><div class="l">overall accuracy, live configuration<br>95% CI {ci(hs['overall_accuracy_ci95'])}</div></div>
 <div class="card kpi"><div class="v">{pct(hs['execution_accuracy'])}</div><div class="l">execution accuracy on the {hs['n_answerable']} answerable questions</div></div>
 <div class="card kpi"><div class="v">{pct(hs['refusal_accuracy'])}</div><div class="l">of the {hs['n_unanswerable']} unanswerable questions correctly refused</div></div>
 <div class="card kpi"><div class="v">{hs['latency_ms_median'] / 1000:.1f}s</div><div class="l">median model time per question (all attempts)</div></div>
</div>

<h2>Accuracy by ablation</h2>
<p class="prose">Each step adds one thing to the one before: <strong>a</strong> {e(ab_labels['a'])}; <strong>b</strong> {e(ab_labels['b'])};
<strong>c</strong> {e(ab_labels['c'])}; <strong>d</strong> {e(ab_labels['d'])}; <strong>e</strong> {e(ab_labels['e'])}. Bars are overall accuracy over all {res['n_questions']} questions; whiskers are 95% bootstrap intervals over questions.</p>
<div class="card chart">{chart_svg(res)}</div>

<div class="card wide" style="margin-top:16px"><table>
<thead><tr><th>Model</th><th>Ablation</th><th class=num>Overall</th><th class=num>95% CI</th><th class=num>Exec. acc.</th><th class=num>strict</th><th class=num>Refusal acc.</th><th class=num>False refusals</th><th class=num>Mean retries</th><th class=num>Median latency</th><th class=num>Prompt tokens</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p class="meta">Exec. acc. = share of the {hs['n_answerable']} answerable questions whose result matched. "strict" counts only the gold definition; the other column also accepts the second definition of a listing count that the warehouse itself carries (see Method). Latency is model time summed over attempts. Run status: {e(statuses)}.</p></div>

<h3>Does each step help?</h3>
<div class="card wide"><table><thead><tr><th>Model</th><th>Step</th><th class=num>Change</th><th class=num>95% CI (paired bootstrap)</th></tr></thead><tbody>{''.join(delta_html)}</tbody></table>
<p class="meta">* the interval excludes zero. With {res['n_questions']} questions, a one-question change is {100 / res['n_questions']:.1f} points, so many step-to-step differences are within noise.</p></div>

<h2>By question type</h2>
<div class="card wide"><table><thead><tr><th>Model, ablation</th>{cat_head}</tr></thead><tbody>{''.join(cat_rows)}</tbody></table></div>

<h2>What goes wrong</h2>
<p class="prose">Failures at ablation {LIVE_ABLATION}, classified by a rule-based tagger (first matching rule wins: refusal errors, execution errors, empty results, missing the documented default filter, wrong tables or join type, date logic, a gold filter literal missing, wrong row count, otherwise wrong calculation). The tagger is a heuristic; the examples below are the evidence.</p>
<div class="card wide"><table><thead><tr><th>Error class</th>{''.join(f'<th class=num>{e(MODEL_LABEL.get(m, m))}</th>' for m in models)}</tr></thead><tbody>{tax_rows}</tbody></table></div>
<h3>Examples</h3>
{''.join(examples)}

<h2>Method</h2>
<div class="prose">
<ul>
<li><strong>Data.</strong> A frozen snapshot of the Data Jobs Observatory warehouse (crawl of {e(res['snapshot_date'])}): 11 tables, the same Parquet files this site loads into your browser.</li>
<li><strong>Gold set.</strong> {g_summary['n_items']} questions in seven types (lookup, aggregation, join, window, date, ambiguous phrasing, unanswerable). Every gold query is executed by <code>make gold-check</code>; {g_summary['n_with_check_sql']} also carry an independently written second query (through a different table or construct) that must return the same result, and {g_summary['n_with_expect']} pin a hand-verified value so a data refresh cannot silently move the target. Ties at ranking cut-offs were checked and avoided.</li>
<li><strong>A finding from verifying the gold set.</strong> The warehouse carries two definitions of "open remote-US listings": the documented default filter (<code>is_open AND remote_us AND is_canonical_listing</code>, 969 listings at 441 companies) and the counters in <code>mart_daily_market</code>/<code>dim_company</code> (1,016 remote listing keys, 448 companies), because the canonical flag picks one posting per listing across all locations. Gold follows the documented filter; for five questions the mart definition is accepted as an alternative and reported separately ("strict" excludes it).</li>
<li><strong>Scoring.</strong> Result sets are compared as multisets; column names and order are ignored and extra columns are allowed; row order is enforced only for ranking questions (and ties may come back in any order); numbers match within 0.1% or when the prediction is the gold value rounded to its own decimals; a fraction and the same value as a percent match. Unanswerable questions are correct only if the agent refuses. A write request is blocked by the read-only guard regardless, but only a refusal scores.</li>
<li><strong>Ablations.</strong> Temperature 0 throughout. Ablation d uses the same prompt as c, so it reuses c's first reply and adds up to two retries when a query errors or returns no rows, feeding the error back. Ablation e adds an answerability rule to the system prompt. It was added after the first run showed refusal was the weakest skill, written from the schema, and checked on a separate 16-question dev set (13/16 correct refusal decisions without it, 15/16 with it for Flash-Lite), never on the gold questions.</li>
<li><strong>Statistics.</strong> 95% intervals are percentile bootstraps over questions (2,000 resamples); step-to-step changes use a paired bootstrap on the same questions.</li>
<li><strong>Cost.</strong> Free-tier Gemini API only, rate-limited and cached by prompt hash, so the whole eval re-scores offline from the committed replies with <code>make eval-offline</code>.</li>
<li><strong>Parity.</strong> The browser app builds its prompts with a JavaScript port of the Python code the eval uses; a test asserts byte-identical prompts for all {g_summary['n_items']} questions and identical guard verdicts.</li>
</ul>
<h3>Limitations</h3>
<ul>
<li>{g_summary['n_items']} questions is small: intervals are wide and a single question moves a score by {100 / res['n_questions']:.1f} points.</li>
<li>The gold SQL, the data-dictionary notes, the retrieval synonyms, the few-shot pool and the answerability rule were all written by the same author. Few-shot examples never answer a gold question (tested), but several are template siblings of one (same shape, different entity), which flatters ablation c. The answerability rule was written after seeing run-1 failures, so treat e as optimistic.</li>
<li>The scorer was corrected after inspecting run 1 (an order-dependent tolerance bug, and a timestamp answering a which-date question); every model was re-scored from the reply cache and no reply changed.</li>
<li>One domain, one warehouse snapshot, English questions only. The "ambiguous" questions encode one reading (the documented default filter); a reasonable analyst could disagree on some.</li>
<li>Execution accuracy can credit a wrong query that happens to return the right numbers, and it does not grade the one-sentence answer the site writes on top of the result.</li>
</ul></div>

<h2>Every question</h2>
<div class="card wide"><table><thead><tr><th>ID</th><th>Question</th><th>Type</th>{q_head}</tr></thead><tbody>{''.join(q_rows)}</tbody></table></div>
</main>
<footer><div class="wrap">Generated from <a href="https://github.com/tachyurgy/datajobs-sql-agent/tree/master/results">results/</a> by <code>askdata/report.py</code>. Data: <a href="https://datajobs.levelbrook.com">Data Jobs Observatory</a>. Built by <a href="https://levelbrook.com">Levelbrook</a>.</div></footer>
<div class="tip" id="tip" role="tooltip"></div>
<script>document.addEventListener("mousemove",function(ev){{var t=ev.target.closest&&ev.target.closest("[data-tip]"),tip=document.getElementById("tip");if(!t){{tip.style.opacity=0;return}}tip.textContent=t.getAttribute("data-tip");tip.style.opacity=1;tip.style.left=Math.min(ev.clientX+12,innerWidth-290)+"px";tip.style.top=ev.clientY+12+"px"}});</script>
<script src="/lb.js" defer></script>
</body></html>
"""
    OUT_HTML.write_text(page)
    OUT_MD.write_text("# askdata eval results\n\nGenerated by `askdata/report.py` from `results/eval*.json`.\n\n"
                      + "\n".join(md) + "\n\n## Step-to-step changes (paired bootstrap)\n\n" + "\n".join(delta_md) + "\n")
    print(OUT_MD.read_text())
    return res


if __name__ == "__main__":
    build()
