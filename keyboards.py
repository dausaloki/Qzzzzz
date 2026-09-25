"""Inline keyboards and shared UI text."""
from __future__ import annotations

import html
from urllib.parse import quote

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M, KeyboardButton,
                      KeyboardButtonPollType, Poll, ReplyKeyboardMarkup, ReplyKeyboardRemove)

import config

PAGE_SIZE = 8


def main_menu() -> M:
    return M([
        [B("➕ New Quiz", callback_data="m:new"), B("📚 My Quizzes", callback_data="m:my")],
        [B("📥 Import", callback_data="m:imp"), B("▶️ Start Quiz", callback_data="m:start")],
        [B("📊 Statistics", callback_data="m:stats"), B("⚙️ Settings", callback_data="m:set")],
        [B("❓ Help", callback_data="m:help")],
    ])


def home_button() -> list:
    return [B("🏠 Main Menu", callback_data="m:home")]


def back_home() -> M:
    return M([home_button()])


# ------------------------------------------------------------ creation
CREATE_QUESTION_BUTTON = "📝 प्रश्न बनाएँ"


def creation_keyboard(has_questions: bool) -> ReplyKeyboardMarkup:
    """Reply keyboard shown while creating a quiz (private chats only).

    The first button opens Telegram's NATIVE quiz-poll creator
    (KeyboardButtonPollType quiz): question, 2–12 options, the correct answer
    and an optional explanation — the poll is then sent to the bot.
    """
    rows = [[KeyboardButton(CREATE_QUESTION_BUTTON, request_poll=KeyboardButtonPollType(type=Poll.QUIZ))]]
    rows.append([KeyboardButton("/done"), KeyboardButton("/undo")] if has_questions
                else [KeyboardButton("/cancel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="प्रश्न poll या text भेजें")


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ------------------------------------------------------------ quiz management
def quiz_card(qid: str, is_owner: bool) -> M:
    """Management screen buttons (owner) / play screen buttons (others)."""
    rows = [[B("▶️ Start Quiz", callback_data=f"q:run:{qid}"), B("📤 Share Quiz", callback_data=f"q:share:{qid}")]]
    if is_owner:
        rows.append([B("✏️ Edit Quiz", callback_data=f"q:edit:{qid}"),
                     B("📊 Statistics", callback_data=f"q:stats:{qid}")])
        rows.append([B("⚙️ Settings", callback_data=f"q:set:{qid}"),
                     B("🗑️ Delete Quiz", callback_data=f"q:del:{qid}")])
    else:
        rows.append([B("📊 Statistics", callback_data=f"q:stats:{qid}")])
    rows.append([B("⬅️ My Quizzes", callback_data="m:my"), *home_button()])
    return M(rows)


def quiz_settings_menu(qid: str, quiz: dict) -> M:
    sq = "ON ✅" if quiz.get("shuffle_questions") else "OFF"
    so = "ON ✅" if quiz.get("shuffle_options") else "OFF"
    return M([
        [B(f"⏱ Timer: {config.timer_label(quiz.get('timer', 0))}", callback_data=f"e:timer:{qid}")],
        [B(f"🔀 Shuffle Questions: {sq}", callback_data=f"e:sq:{qid}")],
        [B(f"🔀 Shuffle Options: {so}", callback_data=f"e:so:{qid}")],
        [B("⬅️ Back", callback_data=f"q:view:{qid}")],
    ])


def ready_markup(qid: str) -> M:
    return M([[B("▶️ मैं तैयार हूँ!", callback_data=f"r:go:{qid}")]])


def confirm_delete(qid: str) -> M:
    return M([[B("✅ हाँ, Delete करें", callback_data=f"q:delok:{qid}"),
               B("❌ नहीं", callback_data=f"q:view:{qid}")]])


def edit_menu(qid: str, quiz: dict) -> M:
    n = quiz.get("question_count", 0)
    return M([
        [B("📝 Edit Title", callback_data=f"e:title:{qid}"), B("📄 Edit Description", callback_data=f"e:desc:{qid}")],
        [B(f"📋 Questions ({n})", callback_data=f"e:list:{qid}:0")],
        [B("➕ Add Question", callback_data=f"e:addq:{qid}")],
        [B("⬅️ Back", callback_data=f"q:view:{qid}")],
    ])


def question_menu(qid: str, question_id: int, pos: int, total: int) -> M:
    rows = [
        [B("✏️ Question text", callback_data=f"e:qt:{qid}:{question_id}"),
         B("🔤 Options", callback_data=f"e:qo:{qid}:{question_id}")],
        [B("✅ Correct answer", callback_data=f"e:qc:{qid}:{question_id}"),
         B("💡 Explanation", callback_data=f"e:explq:{qid}:{question_id}")],
    ]
    move = []
    if pos > 1:
        move.append(B("⬆️ Move up", callback_data=f"e:up:{qid}:{question_id}"))
    if pos < total:
        move.append(B("⬇️ Move down", callback_data=f"e:dn:{qid}:{question_id}"))
    if move:
        rows.append(move)
    rows.append([B("🗑 Delete question", callback_data=f"e:delqx:{qid}:{question_id}")])
    rows.append([B("⬅️ Questions", callback_data=f"e:list:{qid}:{(pos - 1) // PAGE_SIZE}")])
    return M(rows)


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
    base = {"e:delqx": "e:delq", "e:explq": "e:expl", "e:viewq": "e:view", "e:q": "e:list"}.get(action, action)
    if page > 0:
        nav.append(B("◀️", callback_data=f"{base}:{qid}:{page - 1}"))
    if start + PAGE_SIZE < len(questions):
        nav.append(B("▶️", callback_data=f"{base}:{qid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("⬅️ Back", callback_data=back)])
    return M(rows)


def correct_picker(options: list[str], prefix: str = "c:correct", current: int | None = None,
                   cancel: str = "c:cancel") -> M:
    rows = []
    for i, opt in enumerate(options):
        short = opt.replace("\n", " ")
        short = (short[:45] + "…") if len(short) > 45 else short
        mark = "✅ " if current == i else ""
        rows.append([B(f"{mark}{config.OPTION_LABELS[i]}) {short}", callback_data=f"{prefix}:{i}")])
    rows.append([B("❌ Cancel", callback_data=cancel)])
    return M(rows)


def share_markup(link: str, title: str, qid: str | None = None, inline: bool = False,
                 group_url: str = "") -> M:
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('Quiz: ' + title)}"
    rows = [[B("▶️ Start Quiz", url=link)]]
    if group_url:
        rows.append([B("👥 Group में Quiz चलाएँ", url=group_url)])
    rows.append([B("📤 Share to a chat / group", url=share_url)])
    if inline and qid:
        # only offered when the bot really has inline mode enabled (getMe.supports_inline_queries)
        rows.append([B("👥 Quiz card भेजें (inline)", switch_inline_query=f"quiz_{qid}")])
    if qid:
        rows.append([B("⬅️ Quiz", callback_data=f"q:view:{qid}")])
    return M(rows)


def start_privately(link: str) -> M:
    return M([[B("▶️ Start Privately", url=link)]])


def group_card(qid: str, link: str) -> M:
    """Quiz card inside a group: play it here (native quiz polls) or privately."""
    return M([[B("▶️ Start Quiz in this Group", callback_data=f"g:go:{qid}")],
              [B("👤 Start Privately", url=link)]])


def forward_target_menu(existing: bool = True) -> M:
    rows = [[B("➕ New Quiz", callback_data="f:new")]]
    if existing:
        rows[0].append(B("📚 Existing Quiz", callback_data="f:exist"))
    rows.append([B("❌ Cancel", callback_data="c:cancel")])
    return M(rows)


def forward_pick_menu(quizzes: list[dict]) -> M:
    rows = []
    for x in quizzes:
        title = x["title"] if len(x["title"]) <= 40 else x["title"][:39] + "…"
        rows.append([B(f"{title} ({x['question_count']})", callback_data=f"f:pick:{x['id']}")])
    rows.append([B("➕ New Quiz", callback_data="f:new"), B("❌ Cancel", callback_data="c:cancel")])
    return M(rows)


def result_markup(qid: str) -> M:
    return M([
        [B("🔁 Try Again", callback_data=f"r:go:{qid}"), B("📤 Share Quiz", callback_data=f"q:share:{qid}")],
        [B("📊 Statistics", callback_data=f"q:stats:{qid}"), *home_button()],
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
    "/undo — creation में आख़िरी प्रश्न हटाएँ\n"
    "/skip — optional step (description/explanation) या चल रहा प्रश्न skip करें\n"
    "/stop — चल रहा quiz रोकें (partial result save)\n"
    "/cancel — creation/import रद्द करें\n\n"
    "<b>Quiz बनाना</b>\n"
    "/newquiz → नाम → description (/skip) → प्रश्न भेजते जाएँ → /done. अधिकतम 100 प्रश्न।\n"
    "हर प्रश्न: “📝 प्रश्न बनाएँ” button से Telegram quiz poll (2–12 options, 1 सही उत्तर, "
    "optional explanation), या एक text message में प्रश्न + (A) (B)… options (+ optional "
    "'उत्तर: B', 'व्याख्या: …')। सही उत्तर न हो तो bot पूछेगा — कभी अनुमान नहीं लगाता।\n"
    "प्रश्न से पहले कोई text/photo/video भेजें तो वह अगले प्रश्न से पहले दिखाया जाएगा।\n\n"
    "Question में statement, matching, List-I/List-II, assertion-reason, ordering — "
    "कुछ भी multi-line लिख सकते हैं; text exactly वैसा ही save होता है।\n\n"
    "<b>PDF/Text Import</b>\n"
    "PDF (text वाली या scanned — OCR हिंदी+English), .txt file या text भेजें। प्रश्न 'प्रश्न 1.', "
    "'1.', '1)', 'Q1', 'Q.1' से, options (A)/(B)/… (2 से 12) में, और उत्तर 'उत्तर: (A)' / "
    "'Answer: B' या answer-key section में होना चाहिए। Numbering भाग/section में दोबारा 1 से "
    "शुरू हो सकती है; दो-column PDF column-wise पढ़ी जाती है। "
    "Bot कभी उत्तर का अनुमान नहीं लगाता — हर समस्या question number के साथ दिखाई जाती है।\n\n"
    "<b>लंबे प्रश्न</b>\n"
    "Telegram poll में question 300 और option 100 characters तक ही होते हैं। "
    "लंबा होने पर पूरा प्रश्न और पूरे options पहले message में आते हैं, फिर native quiz poll।\n\n"
    "<b>Forward से Quiz</b>\n"
    "किसी channel/group से quiz polls या text MCQs forward करें — सब उसी क्रम में एक ही quiz में जुड़ते हैं; "
    "/done से पूरा करें। Forwarded poll का सही उत्तर Telegram न बताए तो bot पूछता है।\n\n"
    "<b>Group</b>\n"
    "Quiz के 📤 Share से “👥 Group में Quiz चलाएँ” चुनें (या group में <code>/quiz ID</code>), फिर "
    "“▶️ Start Quiz in this Group”. Group में native quiz polls एक-एक करके आते हैं, हर member का उत्तर "
    "अलग save होता है और अंत में leaderboard आता है। /stop — quiz शुरू करने वाला या group admin रोक सकता है। "
    "'👤 Start Privately' से अपना अलग private session भी चला सकते हैं।"
)
