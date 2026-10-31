"""Quiz Access Control, Batch Permissions & Inline Privacy Guard (Feature 6 & 7).

There are no paid or premium quizzes: every quiz is free to play.  A quiz owner
can only choose WHO may play it:

* ``free``    — Open to everyone (default for all existing and new quizzes).
* ``batch``   — Restricted to members of ``quiz['batch_name']`` or authorized
                group chats assigned to that batch (plus quiz owner & admins).
* ``private`` — Restricted strictly to the quiz owner & bot admins; hidden from
                other users in inline queries.
* Authorized chat lists (per-quiz ``allowed_chats_json`` and global
  ``authorized_chats`` table).

A stored tier this module does not know (including ``premium`` / ``paid`` left
by an earlier development build) is treated as ``free`` — the row itself is not
modified.
"""
from __future__ import annotations

import json
from typing import Optional

import config
import database as db
from keyboards import esc

VALID_TIERS = ("free", "batch", "private")

TIER_LABELS = {
    "free": "🌐 Free (Open to All)",
    "batch": "🎓 Batch Restricted",
    "private": "🔒 Private (Owner Only)",
}


def normalize_tier(tier: Optional[str]) -> str:
    t = (tier or "free").strip().lower()
    return t if t in VALID_TIERS else "free"


def tier_label(tier: Optional[str], batch_name: str = "") -> str:
    t = normalize_tier(tier)
    base = TIER_LABELS.get(t, TIER_LABELS["free"])
    if t == "batch" and batch_name:
        return f"{base} ({batch_name})"
    return base


def get_allowed_chats(quiz: Optional[dict]) -> list[int]:
    if not quiz:
        return []
    raw = quiz.get("allowed_chats_json") or "[]"
    if isinstance(raw, list):
        return [int(x) for x in raw if str(x).lstrip("-").isdigit()]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [int(x) for x in parsed if str(x).lstrip("-").isdigit()]
    except Exception:
        pass
    return []


def set_allowed_chats(quiz_id: str, chat_ids: list[int]) -> list[int]:
    clean = sorted({int(c) for c in chat_ids})
    db.update_quiz(quiz_id, allowed_chats_json=json.dumps(clean))
    return clean


def is_privileged(user_id: Optional[int], quiz: Optional[dict]) -> bool:
    if not user_id:
        return False
    uid = int(user_id)
    if config.is_admin(uid):
        return True
    if quiz and int(quiz.get("owner_id") or 0) == uid:
        return True
    return False


def can_share_inline(user_id: Optional[int], quiz: Optional[dict]) -> bool:
    """True when ``user_id`` may see/share ``quiz`` via inline queries.

    Private quizzes are never exposed to non-owners in inline search results.
    """
    if not quiz or quiz.get("status") != "ready":
        return False
    tier = normalize_tier(quiz.get("access_tier"))
    if tier == "private":
        return is_privileged(user_id, quiz)
    return True


def check_quiz_access(
    user_id: Optional[int],
    chat_id: Optional[int],
    quiz: Optional[dict],
) -> tuple[bool, str]:
    """Verify whether ``user_id`` in ``chat_id`` is allowed to start ``quiz``.

    Returns ``(allowed: bool, message: str)``. When ``allowed`` is True,
    ``message`` is empty. When ``allowed`` is False, ``message`` is a clear
    user-facing HTML explanation.
    """
    if not quiz:
        return False, "❌ यह quiz मौजूद नहीं है।"

    uid = int(user_id or 0)
    cid = int(chat_id or uid)

    if is_privileged(uid, quiz):
        return True, ""

    tier = normalize_tier(quiz.get("access_tier"))
    batch_name = (quiz.get("batch_name") or "").strip()
    allowed_chats = get_allowed_chats(quiz)

    # 1. Check Authorized Chat restriction when running in a group/channel (cid != uid)
    if allowed_chats and cid != uid:
        if cid not in allowed_chats and not db.get_authorized_chat(cid):
            return (
                False,
                "⛔ <b>Unauthorized Group Chat</b>\n\n"
                "यह quiz केवल अधिकृत (authorized) Telegram groups में ही चलाया जा सकता है।",
            )

    # 2. Check Private tier
    if tier == "private":
        return (
            False,
            "🔒 <b>Private Quiz</b>\n\n"
            "यह एक निजी (Private) quiz है। केवल इसके निर्माता (creator) ही इसे शुरू कर सकते हैं।",
        )

    # 3. Check Batch tier
    if tier == "batch" or batch_name:
        req_batch = batch_name or "Default"
        if db.user_in_batch(uid, req_batch) or (cid != uid and db.chat_has_batch(cid, req_batch)):
            return True, ""
        return (
            False,
            "🎓 <b>Batch Restricted Quiz</b>\n\n"
            f"यह quiz केवल <b>{esc(req_batch)}</b> बैच के विद्यार्थियों के लिए है।\n"
            "• बैच में जुड़ने के लिए एडमिन से संपर्क करें।",
        )

    return True, ""
