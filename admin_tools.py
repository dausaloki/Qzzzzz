"""Admin & Owner Tools (Feature 7, 8, 9).

Provides:
* Owner/Admin Broadcast messaging (``broadcast_announcement``)
* Admin status dashboard (``admin_dashboard_text``)
* Sanitized error forwarding to ``LOG_GROUP`` (``notify_log_group_error``)
* Display-only community & channel references (``BOT_GROUP``, ``CHANNEL_ID``) on /admin
* Batch and Authorized Chat management helpers
"""
from __future__ import annotations

import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone

from telegram.constants import ParseMode

import ai_gen
import config
import database as db
import membership
from keyboards import esc

log = logging.getLogger("quizbot.admin")
_IST = timezone(timedelta(hours=5, minutes=30))


def effective_log_group() -> str:
    return (os.getenv("LOG_GROUP", "") or getattr(config, "LOG_GROUP", "")).strip()


def effective_bot_group() -> str:
    return (os.getenv("BOT_GROUP", "") or getattr(config, "BOT_GROUP", "")).strip()


def effective_channel_id() -> str:
    return (os.getenv("CHANNEL_ID", "") or getattr(config, "CHANNEL_ID", "")).strip()


_TG_REF_RE = re.compile(r"^(https://t\.me/[A-Za-z0-9_+/]{3,}|@[A-Za-z][A-Za-z0-9_]{4,31}|-100\d{5,})$")


def _ref_status(value: str) -> str:
    """Human label for a display-only Telegram reference (never a silent fallback)."""
    if not value:
        return "Not set"
    if _TG_REF_RE.match(value):
        return value
    return f"{value} ⚠️ invalid format (use https://t.me/..., @username or -100…)"


def _redact_secrets(text: str) -> str:
    """Strip any live token/secret from error logs before sending to LOG_GROUP."""
    out = text or ""
    for env_key in (
        "BOT_TOKEN",
        "GITHUB_TOKEN",
        "OPENROUTER_API_KEY",
        "OPENROUTER_DEFAULT_KEYS",
    ):
        val = (os.getenv(env_key, "") or getattr(config, env_key, "")).strip()
        if val:
            for part in val.split(","):
                p = part.strip()
                if len(p) >= 6:
                    out = out.replace(p, "[REDACTED]")
    return out


async def notify_log_group_error(bot, err: Exception, update: object = None) -> bool:
    """Send a sanitized error report to ``LOG_GROUP`` when configured."""
    target = effective_log_group()
    if not target or bot is None:
        return False
    tb_lines = traceback.format_exception(type(err), err, err.__traceback__)
    short_tb = _redact_secrets("".join(tb_lines[-4:])[:1500])
    err_msg = _redact_secrets(str(err)[:400])
    uid_info = ""
    if update is not None and getattr(update, "effective_user", None):
        uid_info = f"\n👤 User ID: <code>{update.effective_user.id}</code>"
    text = (
        "🚨 <b>Bot Error Alert</b>\n"
        f"🕒 Time: {datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
        f"{uid_info}\n"
        f"⚠️ Type: <code>{esc(type(err).__name__)}</code>\n"
        f"📝 Error: <code>{esc(err_msg)}</code>\n\n"
        f"<pre>{esc(short_tb)}</pre>"
    )
    try:
        await bot.send_message(
            chat_id=int(target) if target.lstrip("-").isdigit() else target,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to send error log to LOG_GROUP (%s): %s", target, exc)
        return False


async def broadcast_announcement(bot, text: str) -> dict:
    """Send an announcement message to all non-banned registered users."""
    msg_text = (text or "").strip()
    if not msg_text:
        raise ValueError("Broadcast संदेश खाली नहीं हो सकता।")
    user_ids = db.list_all_user_ids()
    sent = 0
    failed = 0
    payload = f"📢 <b>Announcement</b>\n\n{msg_text}"
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=payload,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
    return {"total": len(user_ids), "sent": sent, "failed": failed}


def admin_dashboard_text() -> str:
    """Build a secret-free overview of bot status, configuration, and counts."""
    user_ids = db.list_all_user_ids()
    batches = db.list_batches()
    auth_chats = db.list_authorized_chats()
    req_chat = membership.effective_required_chat() or "Disabled"
    log_grp = effective_log_group() or "Disabled"
    bot_grp = _ref_status(effective_bot_group())
    chan_id = _ref_status(effective_channel_id())
    ignored = config.unsupported_env_settings()
    ignored_txt = (
        "".join(f"• ⚠️ <code>{esc(name)}</code> is set but IGNORED — {esc(why)}\n" for name, why in ignored)
        if ignored else ""
    )
    ai_ok = "✅ Enabled" if ai_gen.is_ai_configured() else "⚪ Disabled (No API key)"

    with db.connect() as con:
        quiz_count = con.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0]
        q_count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        att_count = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    return (
        "🛡️ <b>Admin & Owner Control Panel</b>\n"
        f"👑 Owner: <b>{esc(config.OWNER_NAME)}</b> ({esc(config.OWNER_TELEGRAM)})\n\n"
        "📊 <b>Database Summary:</b>\n"
        f"• Registered Users: <b>{len(user_ids)}</b>\n"
        f"• Total Quizzes: <b>{quiz_count}</b> ({q_count} questions)\n"
        f"• Total Attempts: <b>{att_count}</b>\n"
        f"• Batches: <b>{len(batches)}</b> | Authorized Chats: <b>{len(auth_chats)}</b>\n\n"
        "⚙️ <b>Integrations & Security:</b>\n"
        f"• Mandatory Join Gate: <code>{esc(req_chat)}</code>\n"
        f"• AI Quiz Generator (OpenRouter): {ai_ok} (Model: <code>{esc(ai_gen.get_openrouter_model())}</code>)\n"
        f"• Error Log Group: <code>{esc(log_grp)}</code>\n"
        f"• BOT_GROUP (display only): <code>{esc(bot_grp)}</code> | "
        f"CHANNEL_ID (display only): <code>{esc(chan_id)}</code>\n"
        f"• Official group (built-in, PDF footer): <code>{esc(config.OFFICIAL_TELEGRAM_GROUP_URL)}</code>\n"
        f"{ignored_txt}\n"
        "🧰 <b>Admin Commands:</b>\n"
        "• <code>/broadcast &lt;message&gt;</code> — सभी users को सूचना भेजें\n"
        "• <code>/batch create|add|remove|list ...</code> — बैच प्रबंधन\n"
        "• <code>/authchat add|remove|list ...</code> — अधिकृत ग्रुप प्रबंधन\n"
        "• <code>/backup status|verify|export|sync</code> — बैकअप प्रबंधन"
    )
