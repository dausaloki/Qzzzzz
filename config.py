"""Central configuration and Telegram limits."""
from __future__ import annotations

import os
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

MAX_QUESTIONS = 100
MIN_OPTIONS = max(2, int(PollLimit.MIN_OPTION_NUMBER))     # 2 — a quiz question needs a choice (PTB 22.8 reports 1)
MAX_OPTIONS = int(PollLimit.MAX_OPTION_NUMBER)             # 12
OPTION_LABELS = "ABCDEFGHIJKL"[:MAX_OPTIONS]

# Optional Bot API server (e.g. a local telegram-bot-api, or a test fake).
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "").strip().rstrip("/")

TIMER_CHOICES = [10, 15, 30, 60, 90, 120, 180, 300, 0]
DEFAULT_TIMER = 30

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
    return f"{seconds // 60} min"


def get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing. "
            "Set it in Railway → Variables (value from @BotFather)."
        )
    return token
