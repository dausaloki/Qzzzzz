"""Mandatory group membership ("join gate").

Before a user can use the bot they must be a member of ``config.REQUIRED_CHAT``
(default @kalam_kranti).  Pressing "I've Joined" is never trusted: membership
is always verified with the Bot API method ``getChatMember``.

Verified users are cached in the ``users`` table and re-checked with
getChatMember on every /start and after ``MEMBERSHIP_RECHECK_SECONDS``, so a
user who left the group has to join again.  ``chat_member`` updates (delivered
when the bot is an administrator of the group) revoke access immediately.

Requirement: the bot must be a member of the group (administrator for a
channel), otherwise Telegram answers getChatMember with an error and nobody
can be verified — the error is logged for the operator.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from telegram import ChatMember, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.error import TelegramError

import config
import database as db

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER}   # creator/administrator/member

WELCOME_TEXT = ("👋 Welcome!\n\n"
                "Quiz Bot इस्तेमाल करने से पहले हमारे Telegram Group को Join करना जरूरी है।")
NOT_JOINED_TEXT = ("❌ आपने अभी Telegram Group Join नहीं किया है।\n\n"
                   "Please join the group first.")
CHECK_FAILED_TEXT = ("⚠️ अभी Telegram से membership verify नहीं हो सकी।\n\n"
                     "कृपया थोड़ी देर बाद 🔄 I've Joined दबाएँ।")
VERIFIED_TEXT = "✅ Group Join verified successfully!"


def enabled() -> bool:
    return bool(config.REQUIRED_CHAT)


def join_markup(payload: str = "", retry: bool = False) -> M:
    data = "j:check" + (f":{payload}" if payload else "")
    rows = []
    if config.REQUIRED_CHAT_URL:
        rows.append([B("📢 Join Group", url=config.REQUIRED_CHAT_URL)])
    rows.append([B("🔄 I've Joined" if retry else "✅ I've Joined", callback_data=data[:64])])
    return M(rows)


def is_active_member(member) -> bool:
    """Telegram statuses: creator/administrator/member → in the group;
    restricted → only if ``is_member``; left/kicked → not a member."""
    status = getattr(member, "status", None)
    if status in ACTIVE_STATUSES:
        return True
    if status == ChatMember.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def fetch_membership(bot, user_id: int) -> tuple[Optional[bool], str]:
    """(True|False, status) from getChatMember, or (None, error) if Telegram
    could not answer (bot not in the group, network error, …)."""
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHAT, user_id)
    except TelegramError as exc:
        log.warning("getChatMember(%s) failed: %s — is the bot a member/admin of the required group?",
                    config.REQUIRED_CHAT, exc)
        return None, str(exc)
    return is_active_member(member), str(member.status)


async def verify_now(bot, user_id: int) -> Optional[bool]:
    """Always asks Telegram; updates the cache.  None = verification failed."""
    ok, _info = await fetch_membership(bot, user_id)
    if ok is True:
        db.set_member_verified(user_id, time.time())
    elif ok is False:
        db.clear_member(user_id)
    return ok


async def has_access(bot, user_id: int, *, force: bool = False, allow_unverified: bool = False) -> bool:
    """Is this user allowed to use the bot?

    * never verified → False (the user must go through the join screen) unless
      ``allow_unverified`` (group actions, where we check silently);
    * verified recently → True without an API call, unless ``force``;
    * otherwise getChatMember decides.  If Telegram cannot answer (outage /
      misconfiguration) a previously verified user keeps access; a new user
      never gets access without a successful verification.
    """
    if not enabled():
        return True
    verified_ts, checked_ts = db.get_membership(user_id)
    if not verified_ts and not allow_unverified:
        return False
    if verified_ts and not force and time.time() - checked_ts < config.MEMBERSHIP_RECHECK_SECONDS:
        return True
    ok = await verify_now(bot, user_id)
    if ok is None:
        return bool(verified_ts)
    return ok


def is_required_chat(chat) -> bool:
    req = config.REQUIRED_CHAT
    if not req or chat is None:
        return False
    if req.startswith("@"):
        return (chat.username or "").lower() == req[1:].lower()
    try:
        return int(req) == chat.id
    except ValueError:
        return False


async def on_chat_member(update, context) -> None:
    """chat_member update from the required group: revoke access on leave/kick."""
    cmu = update.chat_member
    if cmu is None or not is_required_chat(cmu.chat):
        return
    user = cmu.new_chat_member.user
    if not is_active_member(cmu.new_chat_member):
        db.clear_member(user.id)
        log.info("user %s left the required group — access revoked", user.id)
