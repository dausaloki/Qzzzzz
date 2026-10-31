"""Self-contained Interactive HTML Result Report Generator (Feature 4).

Generates a single-file, offline-capable HTML scorecard and question-by-question
review report featuring:
* Interactive Question Navigator grid (1..N) with status color-coding
* Filter tabs (All / Correct / Wrong / Skipped) and live keyword search
* Full Answer Review with YOUR ANSWER & CORRECT ANSWER badges and explanations
* Section-wise score breakdown (when sections are present)
* Negative marking calculation (supports 1/3 negative marking & per-quiz settings)
* Built-in LaTeX / mathematical formula rendering (fractions, roots, powers,
  subscripts, Greek symbols, operators) + KaTeX compatibility
* Full Unicode / Hindi (Devanagari) typography and Dark/Light theme toggle
* LOKESH DAUSA (@LOKESH_DAUSA) owner branding & official channel link
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import database as db
import quiz_modes
import review_pdf

_IST = timezone(timedelta(hours=5, minutes=30))

_GREEK_MAP = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
    r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ",
    r"\phi": "φ", r"\omega": "ω", r"\Delta": "Δ", r"\Sigma": "∑",
    r"\Omega": "Ω", r"\infty": "∞", r"\times": "×", r"\div": "÷",
    r"\pm": "±", r"\leq": "≤", r"\geq": "≥", r"\neq": "≠",
    r"\approx": "≈", r"\rightarrow": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\int": "∫", r"\sum": "∑", r"\prod": "∏",
    r"\partial": "∂", r"\cdot": "·", r"\degree": "°",
}


def render_math_and_text(raw: str) -> str:
    """Escape HTML safely while converting LaTeX math ($...$, $$...$$, \\(...\\), \\[...\\])
    into rich, self-contained HTML math markup that renders offline and online."""
    if not raw:
        return ""
    text = str(raw)

    def _convert_latex_expr(expr: str, display: bool = False) -> str:
        s = html.escape(expr.strip())
        # \frac{a}{b} -> <span class="math-frac"><span class="num">a</span><span class="den">b</span></span>
        for _ in range(5):
            new_s = re.sub(
                r"\\frac\{([^{}]+)\}\{([^{}]+)\}",
                r'<span class="math-frac"><span class="math-num">\1</span><span class="math-den">\2</span></span>',
                s,
            )
            if new_s == s:
                break
            s = new_s
        # \sqrt{x}
        s = re.sub(
            r"\\sqrt\{([^{}]+)\}",
            r'<span class="math-sqrt">√(<span class="math-rad">\1</span>)</span>',
            s,
        )
        # Superscripts ^{...} or ^x
        s = re.sub(r"\^\{([^{}]+)\}", r"<sup>\1</sup>", s)
        s = re.sub(r"\^([A-Za-z0-9+\-]+)", r"<sup>\1</sup>", s)
        # Subscripts _{...} or _x
        s = re.sub(r"_\{([^{}]+)\}", r"<sub>\1</sub>", s)
        s = re.sub(r"_([A-Za-z0-9+\-]+)", r"<sub>\1</sub>", s)
        for k, v in _GREEK_MAP.items():
            s = s.replace(html.escape(k), v)
        cls = "math-block" if display else "math-inline"
        return f'<span class="{cls}" data-latex="{html.escape(expr.strip())}">{s}</span>'

    # Tokenize math segments so non-math text is plain HTML-escaped
    pattern = re.compile(
        r"(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\$[^\$\n]+?\$|\\\([\s\S]+?\\\))"
    )
    parts = pattern.split(text)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("$$") and part.endswith("$$") and len(part) > 4:
            out.append(_convert_latex_expr(part[2:-2], display=True))
        elif part.startswith(r"\[") and part.endswith(r"\]") and len(part) > 4:
            out.append(_convert_latex_expr(part[2:-2], display=True))
        elif part.startswith("$") and part.endswith("$") and len(part) > 2:
            out.append(_convert_latex_expr(part[1:-1], display=False))
        elif part.startswith(r"\(") and part.endswith(r"\)") and len(part) > 4:
            out.append(_convert_latex_expr(part[2:-2], display=False))
        else:
            out.append(html.escape(part).replace("\n", "<br>"))
    return "".join(out)


def build_html_from_report_data(report: dict, *, negative_mark: Optional[float] = None) -> bytes:
    """Render a ``review_pdf`` report dict into a self-contained interactive HTML document."""
    title = str(report.get("quiz_title") or "Quiz Report")
    quiz_id = str(report.get("quiz_id") or "")
    participant = str(report.get("participant_name") or "User")
    mode_label = str(report.get("mode_label") or "Private Quiz")
    total = int(report.get("total") or 0)
    correct = int(report.get("correct") or 0)
    wrong = int(report.get("wrong") or 0)
    skipped = int(report.get("skipped") or 0)
    unattempted = int(report.get("unattempted") or 0)
    accuracy = float(report.get("accuracy") or 0.0)
    time_taken = str(report.get("time_taken_str") or "0s")
    completed_at = str(
        report.get("completed_at_str")
        or datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
    )

    # Negative marking comes ONLY from the attempt / group-session record (or an
    # explicit argument).  Default = 0 = disabled: wrong answers cost nothing,
    # exactly like the production scoring rules.
    if negative_mark is None:
        neg = quiz_modes.negative_of(report)
    else:
        neg = max(0.0, float(negative_mark or 0.0))

    penalty = round(float(wrong) * neg, 2)
    net_score = quiz_modes.compute_net_score(correct, wrong, neg)
    if neg > 0:
        wrong_lbl = f"❌ Wrong (-{quiz_modes.negative_label(neg)} each)"
        score_val = f"{quiz_modes.format_score(net_score)} / {total}"
        score_lbl = f"🏆 Net Score (-{quiz_modes.format_score(penalty)})"
    else:
        wrong_lbl = "❌ Wrong"
        score_val = f"{correct} / {total}"
        score_lbl = "🏆 Score"

    items = report.get("items") or []
    # Compute section breakdown if sections exist on questions
    sec_Stats: dict[str, dict] = {}
    if quiz_id:
        q_rows = {int(q["id"]): q for q in db.get_questions(quiz_id) if q.get("id") is not None}
    else:
        q_rows = {}

    for it in items:
        qid_num = it.get("question_id")
        sec = (
            it.get("section")
            or (q_rows.get(int(qid_num), {}).get("section") if qid_num else "")
            or ""
        ).strip()
        if sec:
            b = sec_Stats.setdefault(
                sec, {"total": 0, "correct": 0, "wrong": 0, "skipped": 0}
            )
            b["total"] += 1
            st = it.get("status")
            if st == "correct":
                b["correct"] += 1
            elif st == "wrong":
                b["wrong"] += 1
            else:
                b["skipped"] += 1

    nav_buttons_html: list[str] = []
    cards_html: list[str] = []

    for idx, it in enumerate(items, start=1):
        st = str(it.get("status") or "unattempted")
        if st == "correct":
            st_cls = "st-correct"
            st_badge = "✅ Correct"
            filter_cat = "correct"
        elif st == "wrong":
            st_cls = "st-wrong"
            st_badge = "❌ Wrong"
            filter_cat = "wrong"
        elif st in ("skipped", "timeout"):
            st_cls = "st-skipped"
            st_badge = "⏱ Timeout" if st == "timeout" else "⏭ Skipped"
            filter_cat = "skipped"
        else:
            st_cls = "st-unattempted"
            st_badge = "⚪ Not Attempted"
            filter_cat = "skipped"

        nav_buttons_html.append(
            f'<button type="button" class="nav-btn {st_cls}" data-target="qcard-{idx}" '
            f'data-status="{filter_cat}" onclick="jumpToQuestion({idx})">{idx}</button>'
        )

        q_html = render_math_and_text(str(it.get("question") or ""))
        pre_text = str(it.get("pre_text") or "").strip()
        pre_html = (
            f'<div class="q-pre">{render_math_and_text(pre_text)}</div>'
            if pre_text
            else ""
        )

        opts = it.get("options") or []
        chosen = it.get("chosen_index")
        corr = it.get("correct_index")

        opt_rows: list[str] = []
        for o_idx, opt_text in enumerate(opts):
            letter = chr(65 + o_idx) if o_idx < 26 else str(o_idx + 1)
            is_chosen = chosen is not None and int(chosen) == o_idx
            is_corr = corr is not None and int(corr) == o_idx

            row_cls = "opt-row"
            badges: list[str] = []
            if is_corr and is_chosen:
                row_cls += " opt-correct-chosen"
                badges.append('<span class="opt-tag tag-ok">✔ YOUR ANSWER (CORRECT)</span>')
            elif is_corr:
                row_cls += " opt-correct"
                badges.append('<span class="opt-tag tag-corr">✔ CORRECT ANSWER</span>')
            elif is_chosen:
                row_cls += " opt-wrong"
                badges.append('<span class="opt-tag tag-bad">✖ YOUR ANSWER</span>')

            badge_html = " ".join(badges)
            opt_rows.append(
                f'<div class="{row_cls}">'
                f'<span class="opt-letter">({letter})</span>'
                f'<div class="opt-body">'
                f'<div class="opt-text">{render_math_and_text(str(opt_text))}</div>'
                f'{f"<div class=opt-badges>{badge_html}</div>" if badge_html else ""}'
                f"</div></div>"
            )

        expl_raw = str(it.get("explanation") or "").strip()
        expl_html = (
            f'<div class="q-expl"><strong>💡 व्याख्या / Explanation:</strong><br>{render_math_and_text(expl_raw)}</div>'
            if expl_raw
            else ""
        )

        cards_html.append(
            f'<article id="qcard-{idx}" class="q-card {st_cls}" data-status="{filter_cat}" data-qnum="{idx}">'
            f'<header class="q-head">'
            f'<span class="q-num">प्रश्न {idx} / {len(items)}</span>'
            f'<span class="q-status {st_cls}">{st_badge}</span>'
            f"</header>"
            f"{pre_html}"
            f'<div class="q-stem">{q_html}</div>'
            f'<div class="q-options">{"".join(opt_rows)}</div>'
            f"{expl_html}"
            f"</article>"
        )

    sec_table_html = ""
    if sec_Stats:
        srows = []
        for sname, sb in sec_Stats.items():
            s_net = quiz_modes.compute_net_score(sb["correct"], sb["wrong"], neg)
            srows.append(
                f"<tr><td>{html.escape(sname)}</td><td>{sb['total']}</td>"
                f"<td class='c-ok'>{sb['correct']}</td><td class='c-bad'>{sb['wrong']}</td>"
                f"<td>{sb['skipped']}</td><td><strong>{quiz_modes.format_score(s_net)}</strong></td></tr>"
            )
        sec_table_html = (
            '<section class="sec-box"><h2>📂 Section-wise Breakdown</h2>'
            '<table class="sec-table"><thead><tr>'
            "<th>Section</th><th>Total</th><th>Correct</th><th>Wrong</th><th>Skipped</th>"
            f"<th>{'Net Score' if neg > 0 else 'Score'}</th>"
            f"</tr></thead><tbody>{''.join(srows)}</tbody></table></section>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="hi" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Interactive Quiz Report</title>
<style>
:root {{
  --bg: #f4f6fb;
  --card: #ffffff;
  --text: #172033;
  --muted: #5a657d;
  --border: #dde3f0;
  --primary: #2563eb;
  --ok-bg: #ecfdf5;
  --ok-border: #10b981;
  --ok-text: #065f46;
  --bad-bg: #fef2f2;
  --bad-border: #ef4444;
  --bad-text: #991b1b;
  --skip-bg: #fffbeb;
  --skip-border: #f59e0b;
  --skip-text: #92400e;
}}
[data-theme="dark"] {{
  --bg: #0f172a;
  --card: #1e293b;
  --text: #f1f5f9;
  --muted: #94a3b8;
  --border: #334155;
  --primary: #3b82f6;
  --ok-bg: rgba(16, 185, 129, 0.15);
  --ok-border: #10b981;
  --ok-text: #6ee7b7;
  --bad-bg: rgba(239, 68, 68, 0.15);
  --bad-border: #ef4444;
  --bad-text: #fca5a5;
  --skip-bg: rgba(245, 158, 11, 0.15);
  --skip-border: #f59e0b;
  --skip-text: #fcd34d;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 16px;
  background: var(--bg);
  color: var(--text);
  font-family: "Noto Sans Devanagari", "Kohinoor Devanagari", "Mangal", system-ui, -apple-system, sans-serif;
  line-height: 1.6;
}}
.container {{ max-width: 960px; margin: 0 auto; }}
.top-bar {{
  display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
  gap: 12px; background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 18px 22px; margin-bottom: 16px;
}}
.brand-badge {{ font-size: 0.85rem; color: var(--primary); font-weight: 700; }}
.title {{ margin: 4px 0; font-size: 1.45rem; }}
.meta {{ font-size: 0.9rem; color: var(--muted); }}
.btn-tool {{
  cursor: pointer; border: 1px solid var(--border); background: var(--card);
  color: var(--text); padding: 8px 14px; border-radius: 8px; font-weight: 600;
}}
.score-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
  gap: 10px; margin-bottom: 16px;
}}
.stat-card {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 12px 14px; text-align: center;
}}
.stat-val {{ font-size: 1.35rem; font-weight: 800; }}
.stat-lbl {{ font-size: 0.8rem; color: var(--muted); }}
.nav-panel {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 16px; margin-bottom: 16px;
}}
.filter-bar {{
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px;
}}
.filter-btn {{
  cursor: pointer; border: 1px solid var(--border); background: var(--bg);
  color: var(--text); padding: 6px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 600;
}}
.filter-btn.active {{ background: var(--primary); color: #fff; border-color: var(--primary); }}
.search-input {{
  flex: 1; min-width: 180px; padding: 7px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--bg); color: var(--text);
}}
.nav-grid {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.nav-btn {{
  width: 36px; height: 36px; border-radius: 8px; border: 1px solid var(--border);
  cursor: pointer; font-weight: 700; font-size: 0.85rem; background: var(--bg); color: var(--text);
}}
.nav-btn.st-correct {{ background: var(--ok-bg); border-color: var(--ok-border); color: var(--ok-text); }}
.nav-btn.st-wrong {{ background: var(--bad-bg); border-color: var(--bad-border); color: var(--bad-text); }}
.nav-btn.st-skipped, .nav-btn.st-unattempted {{ background: var(--skip-bg); border-color: var(--skip-border); color: var(--skip-text); }}
.q-card {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 18px; margin-bottom: 14px;
}}
.q-head {{
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 12px;
}}
.q-num {{ font-weight: 700; color: var(--primary); }}
.q-status {{ font-size: 0.82rem; font-weight: 700; padding: 3px 10px; border-radius: 12px; }}
.q-status.st-correct {{ background: var(--ok-bg); color: var(--ok-text); }}
.q-status.st-wrong {{ background: var(--bad-bg); color: var(--bad-text); }}
.q-status.st-skipped, .q-status.st-unattempted {{ background: var(--skip-bg); color: var(--skip-text); }}
.q-pre {{
  background: var(--bg); border-left: 3px solid var(--primary);
  padding: 8px 12px; margin-bottom: 10px; font-size: 0.92rem;
}}
.q-stem {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 12px; }}
.opt-row {{
  display: flex; gap: 10px; align-items: flex-start;
  border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 12px; margin-bottom: 8px;
}}
.opt-row.opt-correct, .opt-row.opt-correct-chosen {{
  background: var(--ok-bg); border-color: var(--ok-border);
}}
.opt-row.opt-wrong {{
  background: var(--bad-bg); border-color: var(--bad-border);
}}
.opt-letter {{ font-weight: 700; min-width: 28px; }}
.opt-body {{ flex: 1; }}
.opt-badges {{ margin-top: 4px; }}
.opt-tag {{
  display: inline-block; font-size: 0.75rem; font-weight: 700;
  padding: 2px 8px; border-radius: 6px;
}}
.tag-ok, .tag-corr {{ background: var(--ok-border); color: #fff; }}
.tag-bad {{ background: var(--bad-border); color: #fff; }}
.q-expl {{
  margin-top: 12px; padding: 10px 14px; border-radius: 10px;
  background: var(--bg); border-left: 4px solid var(--primary); font-size: 0.94rem;
}}
.math-inline, .math-block {{
  font-family: "Cambria Math", "STIX Two Math", Georgia, serif;
  padding: 0 2px;
}}
.math-block {{ display: block; text-align: center; margin: 8px 0; font-size: 1.1rem; }}
.math-frac {{
  display: inline-flex; flex-direction: column; vertical-align: middle;
  text-align: center; padding: 0 3px;
}}
.math-num {{ border-bottom: 1px solid currentColor; padding: 0 2px; font-size: 0.88em; }}
.math-den {{ padding: 0 2px; font-size: 0.88em; }}
.sec-box {{
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 16px; margin-bottom: 16px; overflow-x: auto;
}}
.sec-table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
.sec-table th, .sec-table td {{
  border-bottom: 1px solid var(--border); padding: 8px 10px; text-align: left;
}}
.footer {{
  text-align: center; padding: 18px; color: var(--muted); font-size: 0.88rem;
}}
.footer a {{ color: var(--primary); font-weight: 700; text-decoration: none; }}
</style>
</head>
<body>
<div class="container">
  <header class="top-bar">
    <div>
      <div class="brand-badge">👑 QUIZ BOT OWNER: {html.escape(config.QUIZ_BOT_OWNER)} ({html.escape(config.QUIZ_BOT_OWNER_USERNAME)})</div>
      <h1 class="title">📊 {html.escape(title)}</h1>
      <div class="meta">👤 Participant: <strong>{html.escape(participant)}</strong> | 🎯 Mode: {html.escape(mode_label)} | 🕒 {html.escape(completed_at)}</div>
    </div>
    <div>
      <button type="button" class="btn-tool" onclick="toggleTheme()">🌗 Dark / Light</button>
      <button type="button" class="btn-tool" onclick="window.print()">🖨️ Print</button>
    </div>
  </header>

  <section class="score-grid">
    <div class="stat-card"><div class="stat-val">{total}</div><div class="stat-lbl">Total Questions</div></div>
    <div class="stat-card"><div class="stat-val" style="color:var(--ok-border)">{correct}</div><div class="stat-lbl">✅ Correct (+1)</div></div>
    <div class="stat-card"><div class="stat-val" style="color:var(--bad-border)">{wrong}</div><div class="stat-lbl">{html.escape(wrong_lbl)}</div></div>
    <div class="stat-card"><div class="stat-val" style="color:var(--skip-border)">{skipped + unattempted}</div><div class="stat-lbl">⏭ Skipped / Unattempted</div></div>
    <div class="stat-card"><div class="stat-val">{score_val}</div><div class="stat-lbl">{html.escape(score_lbl)}</div></div>
    <div class="stat-card"><div class="stat-val">{accuracy:.1f}%</div><div class="stat-lbl">🎯 Accuracy ({html.escape(time_taken)})</div></div>
  </section>

  {sec_table_html}

  <section class="nav-panel">
    <div class="filter-bar">
      <button type="button" class="filter-btn active" onclick="filterCards('all', this)">All ({len(items)})</button>
      <button type="button" class="filter-btn" onclick="filterCards('correct', this)">✅ Correct ({correct})</button>
      <button type="button" class="filter-btn" onclick="filterCards('wrong', this)">❌ Wrong ({wrong})</button>
      <button type="button" class="filter-btn" onclick="filterCards('skipped', this)">⏭ Skipped ({skipped + unattempted})</button>
      <input type="search" id="q-search" class="search-input" placeholder="🔍 प्रश्न या विकल्प खोजें (Search Hindi / English)..." oninput="searchCards(this.value)">
    </div>
    <div class="nav-grid" id="question-navigator">
      {"".join(nav_buttons_html)}
    </div>
  </section>

  <main id="questions-list">
    {"".join(cards_html)}
  </main>

  <footer class="footer">
    Generated by <strong>{html.escape(config.QUIZ_BOT_OWNER)} ({html.escape(config.QUIZ_BOT_OWNER_USERNAME)})</strong> •
    <a href="{html.escape(config.OFFICIAL_TELEGRAM_GROUP_URL)}" target="_blank" rel="noopener">Join Official Telegram Channel ({html.escape(config.OFFICIAL_TELEGRAM_GROUP_URL)})</a>
  </footer>
</div>

<script>
var currentFilter = 'all';
function toggleTheme() {{
  var root = document.documentElement;
  var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
}}
function jumpToQuestion(qnum) {{
  var el = document.getElementById('qcard-' + qnum);
  if (el) {{
    el.style.display = '';
    el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }}
}}
function filterCards(status, btn) {{
  currentFilter = status;
  var btns = document.querySelectorAll('.filter-btn');
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
  if (btn) btn.classList.add('active');
  applyVisibility();
}}
function searchCards(query) {{
  applyVisibility();
}}
function applyVisibility() {{
  var q = (document.getElementById('q-search').value || '').toLowerCase().trim();
  var cards = document.querySelectorAll('.q-card');
  for (var i = 0; i < cards.length; i++) {{
    var card = cards[i];
    var st = card.getAttribute('data-status');
    var matchesFilter = (currentFilter === 'all' || st === currentFilter);
    var matchesQuery = (!q || card.textContent.toLowerCase().indexOf(q) !== -1);
    card.style.display = (matchesFilter && matchesQuery) ? '' : 'none';
  }}
}}
</script>
</body>
</html>"""
    return doc.encode("utf-8")


def _normalize_report_obj(rep_obj, quiz_id: str = "", duration_sec: float = 0.0) -> dict:
    """Convert a ``review_pdf.Report`` dataclass (or dict) into a normalized dict."""
    if isinstance(rep_obj, dict):
        return rep_obj
    questions_list = getattr(rep_obj, "questions", []) or []
    q_rows = db.get_questions(quiz_id) if quiz_id else []
    items = []
    for idx, rq in enumerate(questions_list):
        corr_set = getattr(rq, "correct", None) or set()
        corr_idx = min(corr_set) if corr_set else 0
        raw_st = str(getattr(rq, "status", "unanswered") or "unanswered").lower()
        if raw_st == "correct":
            st = "correct"
        elif raw_st in ("wrong", "incorrect"):
            st = "wrong"
        elif getattr(rq, "skipped", False):
            st = "skipped"
        else:
            st = "unattempted"
        sec = getattr(rq, "section", "") or ""
        if not sec and idx < len(q_rows):
            sec = q_rows[idx].get("section") or ""
        items.append({
            "question_id": q_rows[idx]["id"] if idx < len(q_rows) else None,
            "number": getattr(rq, "number", idx + 1),
            "question": getattr(rq, "text", ""),
            "options": list(getattr(rq, "options", []) or []),
            "chosen_index": getattr(rq, "chosen", None),
            "correct_index": corr_idx,
            "status": st,
            "explanation": getattr(rq, "explanation", "") or "",
            "pre_text": getattr(rq, "pre_text", "") or "",
            "section": sec,
        })
    secs = int(round(float(duration_sec or 0)))
    time_str = f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"
    return {
        "quiz_id": quiz_id,
        "quiz_title": getattr(rep_obj, "quiz_title", "Quiz"),
        "participant_name": getattr(rep_obj, "user_label", "User"),
        "mode_label": "Group Quiz" if getattr(rep_obj, "mode", "") == "group" else "Private Quiz",
        "total": int(getattr(rep_obj, "total", len(items))),
        "correct": int(getattr(rep_obj, "correct", 0)),
        "wrong": int(getattr(rep_obj, "incorrect", 0)),
        "skipped": int(getattr(rep_obj, "skipped", 0)),
        "unattempted": max(0, int(getattr(rep_obj, "unanswered", 0)) - int(getattr(rep_obj, "skipped", 0))),
        "accuracy": float(getattr(rep_obj, "percentage", 0.0)),
        "time_taken_str": time_str,
        "completed_at_str": getattr(rep_obj, "when_local", ""),
        "negative_mark": float(getattr(rep_obj, "negative_mark", 0.0) or 0.0),
        "items": items,
    }


def build_html_report_for_attempt(attempt_or_id) -> tuple[bytes, str, dict]:
    """Build interactive HTML report ``(html_bytes, filename, report_dict)`` for a private attempt."""
    att = attempt_or_id if isinstance(attempt_or_id, dict) else db.get_attempt(int(attempt_or_id))
    if not att:
        raise ValueError("Attempt report not found")
    rep_obj = review_pdf.report_for_attempt(att)
    rep = _normalize_report_obj(rep_obj, quiz_id=str(att.get("quiz_id") or ""), duration_sec=float(att.get("duration_sec") or 0))
    html_bytes = build_html_from_report_data(rep)
    safe_title = re.sub(r"[^\w\u0900-\u097F\-]+", "_", rep.get("quiz_title") or "Quiz").strip("_")[:40] or "Quiz"
    filename = f"{safe_title}_Report_{att['id']}.html"
    return html_bytes, filename, rep


def build_html_report_for_group(session_or_id, user_id: int, user=None) -> tuple[bytes, str, dict]:
    """Build interactive HTML report ``(html_bytes, filename, report_dict)`` for a group participant."""
    sess = session_or_id if isinstance(session_or_id, dict) else db.get_group_session(int(session_or_id))
    if not sess:
        raise ValueError("Group session report not found")
    rep_obj = review_pdf.report_for_group(sess, user_id)
    dur = max(0.0, float(sess.get("finished_ts") or 0) - float(sess.get("started_ts") or 0))
    rep = _normalize_report_obj(rep_obj, quiz_id=str(sess.get("quiz_id") or ""), duration_sec=dur)
    html_bytes = build_html_from_report_data(rep)
    safe_title = re.sub(r"[^\w\u0900-\u097F\-]+", "_", rep.get("quiz_title") or "Quiz").strip("_")[:40] or "Quiz"
    filename = f"{safe_title}_GroupReport_{sess['id']}_{user_id}.html"
    return html_bytes, filename, rep


def build_latest_html_report(user_id: int, quiz_id: Optional[str] = None, user=None) -> Optional[tuple[bytes, str, dict]]:
    """Build interactive HTML report for a user's most recent private or group attempt."""
    att = db.latest_answered_attempt(user_id, quiz_id) or db.get_latest_completed_attempt(user_id, quiz_id)
    gs = db.latest_answered_group_session(user_id, quiz_id)
    if att and gs:
        att_ts = float(att.get("started_ts") or 0) + float(att.get("duration_sec") or 0)
        gs_ts = float(gs.get("finished_ts") or gs.get("started_ts") or 0)
        if gs_ts > att_ts:
            return build_html_report_for_group(gs, user_id, user=user)
        return build_html_report_for_attempt(att)
    if att:
        return build_html_report_for_attempt(att)
    if gs:
        return build_html_report_for_group(gs, user_id, user=user)
    return None
