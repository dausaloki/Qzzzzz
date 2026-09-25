"""Inline keyboards and shared UI text."""
from __future__ import annotations

import html
from urllib.parse import quote

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import config

PAGE_SIZE = 8


def main_menu() -> M:
    return M([
        [B("➕ New Quiz", callback_data="m:new"), B("📚 My Quizzes", callback_data="m:my")],
        [B("📥 Import PDF/Text", callback_data="m:imp"), B("▶️ Start Quiz", callback_data="m:start")],
        [B("📊 Quiz Stats", callback_data="m:stats"), B("⚙️ Settings", callback_data="m:set")],
        [B("ℹ️ Help", callback_data="m:help")],
    ])


def home_button() -> list:
    return [B("🏠 Main Menu", callback_data="m:home")]


def back_home() -> M:
    return M([home_button()])


def quiz_card(qid: str, is_owner: bool) -> M:
    rows = [[B("▶ Start", callback_data=f"q:run:{qid}"), B("🔗 Share", callback_data=f"q:share:{qid}")]]
    if is_owner:
        rows.append([B("✏️ Edit", callback_data=f"q:edit:{qid}"), B("📊 Stats", callback_data=f"q:stats:{qid}")])
        rows.append([B("🗑 Delete", callback_data=f"q:del:{qid}")])
    else:
        rows.append([B("📊 Stats", callback_data=f"q:stats:{qid}")])
    rows.append([B("⬅️ My Quizzes", callback_data="m:my"), *home_button()])
    return M(rows)


def confirm_delete(qid: str) -> M:
    return M([[B("✅ हाँ, Delete करें", callback_data=f"q:delok:{qid}"),
               B("❌ नहीं", callback_data=f"q:view:{qid}")]])


def edit_menu(qid: str, quiz: dict) -> M:
    sq = "ON ✅" if quiz.get("shuffle_questions") else "OFF"
    so = "ON ✅" if quiz.get("shuffle_options") else "OFF"
    return M([
        [B("📝 Edit Title", callback_data=f"e:title:{qid}"), B("📄 Edit Description", callback_data=f"e:desc:{qid}")],
        [B("➕ Add Question", callback_data=f"e:addq:{qid}"), B("🗑 Delete Question", callback_data=f"e:delq:{qid}:0")],
        [B("💡 Edit Explanation", callback_data=f"e:expl:{qid}:0"), B("👁 View Questions", callback_data=f"e:view:{qid}:0")],
        [B(f"⏱ Timer: {config.timer_label(quiz.get('timer', 0))}", callback_data=f"e:timer:{qid}")],
        [B(f"🔀 Shuffle Questions: {sq}", callback_data=f"e:sq:{qid}")],
        [B(f"🔀 Shuffle Options: {so}", callback_data=f"e:so:{qid}")],
        [B("⬅️ Back", callback_data=f"q:view:{qid}")],
    ])


def timer_menu(prefix: str, current: int, back: str) -> M:
    """prefix: callback prefix, e.g. 'e:settimer:<qid>' or 's:settimer'."""
    labels = {10: "10s", 15: "15s", 30: "30s", 60: "60s", 90: "90s",
              120: "2 min", 180: "3 min", 300: "5 min", 0: "No timer"}
    rows, row = [], []
    for v in config.TIMER_CHOICES:
        mark = "✅ " if int(current or 0) == v else ""
        row.append(B(mark + labels[v], callback_data=f"{prefix}:{v}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("⬅️ Back", callback_data=back)])
    return M(rows)


def question_picker(qid: str, questions: list[dict], page: int, action: str, back: str) -> M:
    """Paginated list of questions; callback f'{action}:{qid}:{question_id}'."""
    start = page * PAGE_SIZE
    rows = []
    for i, q in enumerate(questions[start:start + PAGE_SIZE], start + 1):
        label = q["question"].replace("\n", " ")
        label = (label[:40] + "…") if len(label) > 40 else label
        rows.append([B(f"{i}. {label}", callback_data=f"{action}:{qid}:{q['id']}")])
    nav = []
    base = {"e:delqx": "e:delq", "e:explq": "e:expl", "e:viewq": "e:view"}.get(action, action)
    if page > 0:
        nav.append(B("◀️", callback_data=f"{base}:{qid}:{page - 1}"))
    if start + PAGE_SIZE < len(questions):
        nav.append(B("▶️", callback_data=f"{base}:{qid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("⬅️ Back", callback_data=back)])
    return M(rows)


def correct_picker(options: list[str], prefix: str = "c:correct") -> M:
    rows = []
    for i, opt in enumerate(options):
        short = opt.replace("\n", " ")
        short = (short[:45] + "…") if len(short) > 45 else short
        rows.append([B(f"{config.OPTION_LABELS[i]}) {short}", callback_data=f"{prefix}:{i}")])
    rows.append([B("❌ Cancel", callback_data="c:cancel")])
    return M(rows)


def share_markup(link: str, title: str) -> M:
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('Quiz: ' + title)}"
    return M([
        [B("▶️ Start Quiz", url=link)],
        [B("📤 Share to a chat / group", url=share_url)],
    ])


def start_privately(link: str) -> M:
    return M([[B("▶️ Start Privately", url=link)]])


def result_markup(qid: str) -> M:
    return M([
        [B("🔁 Restart", callback_data=f"q:run:{qid}"), B("📊 Stats", callback_data=f"q:stats:{qid}")],
        home_button(),
    ])


def running_markup() -> M:
    return M([[B("⏭ Skip", callback_data="r:skip"), B("⏹ Stop", callback_data="r:stop")]])


def settings_menu(user: dict) -> M:
    sq = "ON ✅" if user.get("default_shuffle_questions") else "OFF"
    so = "ON ✅" if user.get("default_shuffle_options") else "OFF"
    return M([
        [B(f"⏱ Default Timer: {config.timer_label(user.get('default_timer', config.DEFAULT_TIMER))}",
           callback_data="s:timer")],
        [B(f"🔀 Default Shuffle Questions: {sq}", callback_data="s:sq")],
        [B(f"🔀 Default Shuffle Options: {so}", callback_data="s:so")],
        home_button(),
    ])


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


HELP_TEXT = (
    "🤖 <b>Quiz Bot — Help</b>\n\n"
    "<b>Commands</b>\n"
    "/start — main menu\n"
    "/newquiz — नया quiz बनाएँ\n"
    "/myquizzes — आपके quizzes\n"
    "/import — PDF/Text से quiz\n"
    "/stats — आपकी result history\n"
    "/done — quiz creation पूरा करें\n"
    "/skip — current question skip करें\n"
    "/stop — चल रहा quiz रोकें (partial result save)\n"
    "/cancel — creation/import रद्द करें\n\n"
    "<b>Quiz बनाना</b>\n"
    "Name → Description → (optional text/media) → Question → 4 options → सही उत्तर → "
    "(optional) explanation → और प्रश्न / /done. अधिकतम 100 प्रश्न।\n\n"
    "Question में statement, matching, List-I/List-II, assertion-reason, ordering — "
    "कुछ भी multi-line लिख सकते हैं; text exactly वैसा ही save होता है।\n\n"
    "<b>PDF/Text Import</b>\n"
    "Selectable-text PDF, .txt file या text भेजें। प्रश्न 'प्रश्न 1.', '1.', '1)', 'Q1', 'Q.1' से, "
    "options (A)-(D) में, और उत्तर 'उत्तर: (A)' / 'Answer: B' या answer-key section में होना चाहिए। "
    "Bot कभी उत्तर का अनुमान नहीं लगाता — हर समस्या question number के साथ दिखाई जाती है।\n\n"
    "<b>लंबे प्रश्न</b>\n"
    "Telegram poll में question 300 और option 100 characters तक ही होते हैं। "
    "लंबा होने पर पूरा प्रश्न और पूरे options पहले message में आते हैं, फिर native quiz poll।\n\n"
    "<b>Group</b>\n"
    "Group में 'Start Privately' button आता है — हर member का अपना अलग private session चलता है।"
)
