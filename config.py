"""Central configuration and Telegram limits."""
from __future__ import annotations

import os
import re
from pathlib import Path

from telegram.constants import MessageLimit, PollLimit


def _data_dir() -> Path:
    # Railway sets RAILWAY_VOLUME_MOUNT_PATH automatically when a volume is attached.
    explicit = os.getenv("DATA_DIR", "").strip()
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    base = Path(explicit or volume or Path(__file__).resolve().parent / "data")
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA_DIR = _data_dir()
DB_PATH = Path(os.getenv("DB_PATH", "").strip() or DATA_DIR / "quiz.db")
LOCK_PATH = DATA_DIR / "bot.lock"

# Telegram native poll limits (Bot API 9.2 / PTB 22.5)
POLL_QUESTION_MAX = int(PollLimit.MAX_QUESTION_LENGTH)      # 300
POLL_OPTION_MAX = int(PollLimit.MAX_OPTION_LENGTH)          # 100
POLL_EXPLANATION_MAX = int(PollLimit.MAX_EXPLANATION_LENGTH)  # 200
POLL_EXPLANATION_MAX_NEWLINES = 2
MESSAGE_MAX = int(MessageLimit.MAX_TEXT_LENGTH)             # 4096
CAPTION_MAX = int(MessageLimit.CAPTION_LENGTH)              # 1024
SAFE_MESSAGE_CHUNK = 4000

# A quiz ("part") holds at most this many questions.  There is NO limit on the
# total: bigger imports are split automatically into "<title> — Part 1/2/3…"
# (Part 1 = Q1–Q500, Part 2 = Q501–Q1000, …) linked by one series id.
QUESTIONS_PER_PART = max(1, int(os.getenv("QUESTIONS_PER_PART", "500")))
MIN_OPTIONS = max(2, int(PollLimit.MIN_OPTION_NUMBER))     # 2 — a quiz question needs a choice (PTB 22.8 reports 1)
MAX_OPTIONS = int(PollLimit.MAX_OPTION_NUMBER)             # 12
OPTION_LABELS = "ABCDEFGHIJKL"[:MAX_OPTIONS]

# Optional Bot API server (e.g. a local telegram-bot-api, or a test fake).
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/")

TIMER_CHOICES = [10, 15, 30, 60, 90, 120, 180, 300, 0]
DEFAULT_TIMER = 30
# "✏️ Custom Time": any whole number of seconds in this range (0 = No timer).
# Telegram's poll open_period only accepts 5–600 s; longer timers are enforced
# by the bot's own job, which closes the poll with stopPoll.
CUSTOM_TIMER_MIN = int(os.getenv("CUSTOM_TIMER_MIN", "5"))
CUSTOM_TIMER_MAX = int(os.getenv("CUSTOM_TIMER_MAX", "3600"))

# Delay between an answer and the next question, so the user sees the native
# correct/wrong animation.
NEXT_QUESTION_DELAY = float(os.getenv("NEXT_QUESTION_DELAY", "1.5"))
# Extra grace after open_period before we mark the question as skipped.
TIMER_GRACE = float(os.getenv("TIMER_GRACE", "1.0"))
# Active sessions older than this are expired on startup.
SESSION_EXPIRY_HOURS = int(os.getenv("SESSION_EXPIRY_HOURS", "24"))
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API getFile limit

# ---------------------------------------------------------------- question tag
# Prepended (once) to EVERY question the bot sends — private and group polls
# and the full-text message used for long questions.  Empty string disables it.
QUESTION_TAG = os.getenv("QUESTION_TAG", "जय श्री श्याम").strip()

# ---------------------------------------------------------------- join gate
# Users must be members of this group/channel before they can use the bot.
# "@username" or a numeric chat id (-100…).  The bot must be a member of the
# group (for a channel: an administrator), otherwise Telegram's getChatMember
# cannot report membership.  Empty string disables the gate.
REQUIRED_CHAT = os.getenv("REQUIRED_CHAT", "@kalam_kranti").strip()
REQUIRED_CHAT_URL = os.getenv("REQUIRED_CHAT_URL", "").strip() or (
    f"https://t.me/{REQUIRED_CHAT.lstrip('@')}" if REQUIRED_CHAT.startswith("@") else "")
# A verified membership is re-checked with getChatMember after this many seconds
# (and on every /start), so users who left the group must join again.
MEMBERSHIP_RECHECK_SECONDS = int(os.getenv("MEMBERSHIP_RECHECK_SECONDS", "600"))

# ---------------------------------------------------------------- creation
# The "📝 प्रश्न बनाएँ" reply button opens Telegram's own poll editor, which
# limits the question to 300 characters and shows its own "-57" style counter
# (a Telegram app feature the bot cannot change).  Off by default: questions
# are typed/pasted as text (no length limit); polls can still be sent/forwarded.
NATIVE_POLL_BUTTON = os.getenv("NATIVE_POLL_BUTTON", "0").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------- group quizzes
# Seconds per question in a group when the quiz itself has "No timer"
# (a group quiz needs a time limit to move on to the next question).
GROUP_DEFAULT_TIMER = int(os.getenv("GROUP_DEFAULT_TIMER", "30"))
# Pause between a group question closing and the next one.
GROUP_NEXT_DELAY = float(os.getenv("GROUP_NEXT_DELAY", "2.0"))


def timer_label(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "No timer"
    if seconds < 60 or seconds == 90:
        return f"{seconds} sec"
    m, s = divmod(seconds, 60)
    return f"{m} min" if not s else f"{m} min {s} sec"


_SEC_WORDS = r"(?:s|sec|secs|second|seconds|सेकंड|सेकेंड|सेकण्ड)"
_MIN_WORDS = r"(?:m|min|mins|minute|minutes|मिनट)"


def parse_timer(text: str) -> int:
    """Parse a custom timer: "45", "45 sec", "2 min", "2m 30s", "1:30", "90 सेकंड",
    "3 मिनट", "0"/"no timer" (= no timer).  Raises ValueError with a Hindi message."""
    t = " ".join(str(text or "").strip().lower().split())
    if t in ("0", "no timer", "off", "none", "कोई नहीं", "टाइमर नहीं"):
        return 0
    m = (re.fullmatch(rf"(\d{{1,5}})\s*{_SEC_WORDS}?", t) or None)
    if m:
        val = int(m.group(1))
    elif (m := re.fullmatch(rf"(\d{{1,4}})\s*{_MIN_WORDS}", t)):
        val = int(m.group(1)) * 60
    elif (m := re.fullmatch(rf"(\d{{1,4}})\s*{_MIN_WORDS}\s*(\d{{1,2}})\s*{_SEC_WORDS}?", t)):
        val = int(m.group(1)) * 60 + int(m.group(2))
    elif (m := re.fullmatch(r"(\d{1,4}):([0-5]\d)", t)):
        val = int(m.group(1)) * 60 + int(m.group(2))
    else:
        raise ValueError("समय समझ नहीं आया। ऐसे भेजें: 45 या 45 sec, 2 min, 1:30, 2m 30s (0 = No timer)")
    if val == 0:
        return 0
    if not CUSTOM_TIMER_MIN <= val <= CUSTOM_TIMER_MAX:
        raise ValueError(f"Timer {CUSTOM_TIMER_MIN} sec से {timer_label(CUSTOM_TIMER_MAX)} के बीच होना चाहिए "
                         "(या 0 = No timer)।")
    return val


def get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing. "
            "Set it in Railway → Variables (value from @BotFather)."
        )
    return token
