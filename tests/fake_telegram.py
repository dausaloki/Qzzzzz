"""A fake Telegram Bot API used by the integration tests.

It plugs into python-telegram-bot at the HTTP layer (BaseRequest), so the real
bot code, handlers, JobQueue and database run unmodified. It also enforces
Telegram's real limits on polls so an oversized poll fails the test exactly
like production would.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any, Optional

from telegram import Update
from telegram.request import BaseRequest, RequestData

_GLOBAL_POLL_IDS = itertools.count(5000)  # Telegram poll ids are globally unique
BOT_USER = {"id": 999000, "is_bot": True, "first_name": "Quiz Bot", "username": "TestQuizBot",
            "supports_inline_queries": True}


def _markup(p: dict) -> dict:
    rm = p.get("reply_markup")
    if isinstance(rm, str):
        try:
            rm = json.loads(rm)
        except ValueError:
            rm = None
    return rm or {}


def _check_markup(api: str, p: dict) -> None:
    """Enforce the Bot API rules the bot relies on for reply keyboards."""
    rm = _markup(p)
    if "keyboard" in rm:
        if api.startswith("edit"):
            # only inline keyboards can be attached by editing a message
            raise _ApiError(400, "Bad Request: inline keyboard expected")
        if int(p.get("chat_id", 0)) < 0 and any(b.get("request_poll") for row in rm["keyboard"]
                                                for b in row if isinstance(b, dict)):
            raise _ApiError(400, "Bad Request: keyboard button request poll is allowed only in private chats")



def _u16(s: str) -> int:
    """Telegram measures text in UTF-16 code units (emoji / 𝑥 count as 2)."""
    return len(str(s).encode("utf-16-le")) // 2

class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._msg_ids = itertools.count(1000)
        self._poll_ids = _GLOBAL_POLL_IDS
        self.polls: dict[str, dict] = {}          # poll_id -> poll info (+chat, message_id)
        self.files: dict[str, bytes] = {}         # file_id -> bytes
        self.conflicts_left = 0                   # getUpdates → 409 this many times
        self.get_updates_calls = 0
        self.fail_next: dict[str, str] = {}       # method -> error description (once)
        self.rejections: list[tuple[str, str]] = []  # (method, description) of every real 4xx
        self.inline_answers: list[dict] = []      # answerInlineQuery payloads
        # getChatMember: (chat_id as sent, user_id) -> status / member dict
        self.members: dict[tuple[str, int], Any] = {}
        self.default_member_status = "member"     # for users not listed in ``members``
        self.member_error: Optional[str] = None   # make getChatMember fail (bot not in group…)

    # -- BaseRequest interface ---------------------------------------------------
    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> Optional[float]:
        return 5.0

    async def do_request(self, url: str, method: str, request_data: Optional[RequestData] = None,
                         read_timeout=None, write_timeout=None, connect_timeout=None,
                         pool_timeout=None) -> tuple[int, bytes]:
        if "/file/bot" in url:
            file_path = url.split("/file/bot", 1)[1].split("/", 1)[1]
            fid = file_path.split("/")[-1]
            return 200, self.files[fid]
        api = url.rsplit("/", 1)[-1]
        params: dict[str, Any] = {}
        if request_data is not None:
            params = dict(request_data.parameters)
        self.calls.append((api, params))
        if api in self.fail_next:
            desc = self.fail_next.pop(api)
            return 400, json.dumps({"ok": False, "error_code": 400, "description": desc}).encode()
        try:
            result = await self._dispatch(api, params)
        except _ApiError as exc:
            self.rejections.append((api, exc.description))
            return exc.code, json.dumps({"ok": False, "error_code": exc.code,
                                         "description": exc.description}).encode()
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # -- API emulation ------------------------------------------------------------
    def _message(self, chat_id, **extra) -> dict:
        chat_id = int(chat_id)
        chat = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}
        if chat_id < 0:
            chat["title"] = "Test Group"
        else:
            chat["first_name"] = "User"
        m = {"message_id": next(self._msg_ids), "date": int(time.time()), "chat": chat,
             "from": BOT_USER}
        m.update(extra)
        return m

    async def _dispatch(self, api: str, p: dict):
        if api == "getMe":
            return BOT_USER
        if api in ("setMyCommands", "deleteWebhook", "answerCallbackQuery", "deleteMessage"):
            return True
        if api == "getUpdates":
            self.get_updates_calls += 1
            if self.conflicts_left > 0:
                self.conflicts_left -= 1
                raise _ApiError(409, "Conflict: terminated by other getUpdates request; "
                                     "make sure that only one bot instance is running")
            await asyncio.sleep(0.05)
            return []
        if api in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            _check_markup(api, p)
        if api == "answerInlineQuery":
            res = p.get("results", [])
            res = json.loads(res) if isinstance(res, str) else res
            res = [json.loads(r) if isinstance(r, str) else r for r in res]
            if len(res) > 50:
                raise _ApiError(400, "Bad Request: too many inline query results")
            btn = p.get("button")
            self.inline_answers.append({"inline_query_id": p.get("inline_query_id"), "results": res,
                                        "is_personal": p.get("is_personal"),
                                        "button": json.loads(btn) if isinstance(btn, str) else btn})
            return True
        if api == "sendMessage":
            text = p.get("text", "")
            if not text:
                raise _ApiError(400, "Bad Request: message text is empty")
            if _u16(text) > 4096:
                raise _ApiError(400, "Bad Request: message is too long")
            return self._message(p["chat_id"], text=text)
        if api in ("editMessageText",):
            if _u16(p.get("text", "")) > 4096:
                raise _ApiError(400, "Bad Request: message is too long")
            return self._message(p.get("chat_id", 1), text=p.get("text", ""))
        if api == "editMessageReplyMarkup":
            return self._message(p.get("chat_id", 1), text="x")
        if api in ("sendPhoto", "sendVideo", "sendDocument", "sendAnimation", "sendAudio", "sendVoice"):
            cap = p.get("caption") or ""
            if _u16(cap) > 1024:
                raise _ApiError(400, "Bad Request: message caption is too long")
            return self._message(p["chat_id"], caption=cap)
        if api == "sendPoll":
            return self._send_poll(p)
        if api == "stopPoll":
            for pid, poll in self.polls.items():
                if poll["message_id"] == int(p["message_id"]):
                    poll["is_closed"] = True
                    return self._poll_obj(pid)
            raise _ApiError(400, "Bad Request: poll has already been closed")
        if api == "getChatMember":
            return self._chat_member(p)
        if api == "getFile":
            fid = p["file_id"]
            return {"file_id": fid, "file_unique_id": "u" + fid, "file_size": len(self.files.get(fid, b"")),
                    "file_path": f"documents/{fid}"}
        raise _ApiError(400, f"Bad Request: method {api} not emulated")

    def _send_poll(self, p: dict) -> dict:
        q = p.get("question", "")
        opts = p.get("options", [])
        opts = [json.loads(o) if isinstance(o, str) and o.startswith("{") else o for o in
                (json.loads(opts) if isinstance(opts, str) else opts)]
        texts = [o["text"] if isinstance(o, dict) else str(o) for o in opts]
        if not 1 <= _u16(q) <= 300:
            raise _ApiError(400, "Bad Request: poll question length must not exceed 300")
        if not 2 <= len(texts) <= 12:          # Bot API 9.1+: up to 12 options
            raise _ApiError(400, "Bad Request: poll must have 2-12 options")
        for t in texts:
            if not 1 <= _u16(t) <= 100 or "\n" in t:
                raise _ApiError(400, "Bad Request: poll options length must not exceed 100")
        expl = p.get("explanation")
        if expl and _u16(expl) > 200:
            raise _ApiError(400, "Bad Request: explanation is too long")
        ids = None
        if p.get("type") == "quiz":
            # Bot API 9.6: correct_option_ids (array); the old scalar is still accepted
            ids = p.get("correct_option_ids")
            if isinstance(ids, str):
                ids = json.loads(ids)
            if ids is None and p.get("correct_option_id") is not None:
                ids = [int(p["correct_option_id"])]
            if not ids or any(not 0 <= int(i) < len(texts) for i in ids):
                raise _ApiError(400, "Bad Request: wrong correct option id")
            ids = [int(i) for i in ids]
        op = p.get("open_period")
        if op is not None and not 5 <= int(op) <= 600:
            raise _ApiError(400, "Bad Request: open_period must be 5..600")
        pid = str(next(self._poll_ids))
        msg = self._message(p["chat_id"])
        anon = p.get("is_anonymous")
        anon = True if anon is None else (anon in (True, "true", "True", 1))
        self.polls[pid] = {"question": q, "options": texts, "correct_option_id": ids[0] if ids else None,
                           "correct_option_ids": ids, "type": p.get("type") or "regular", "is_anonymous": anon,
                           "chat_id": int(p["chat_id"]), "message_id": msg["message_id"], "is_closed": False,
                           "explanation": expl, "open_period": op, "reply_markup": p.get("reply_markup")}
        msg["poll"] = self._poll_obj(pid)
        return msg

    def _poll_obj(self, pid: str) -> dict:
        poll = self.polls[pid]
        return {"id": pid, "question": poll["question"],
                "options": [{"text": t, "voter_count": 0, "persistent_id": f"{pid}-{i}"}
                            for i, t in enumerate(poll["options"])],
                "total_voter_count": 0, "is_closed": poll["is_closed"], "is_anonymous": poll["is_anonymous"],
                "type": poll["type"], "allows_multiple_answers": False, "allows_revoting": False,
                "members_only": False, "correct_option_ids": poll["correct_option_ids"]}

    def _chat_member(self, p: dict) -> dict:
        if self.member_error:
            raise _ApiError(400, self.member_error)
        uid = int(p["user_id"])
        st = self.members.get((str(p["chat_id"]), uid), self.default_member_status)
        if isinstance(st, dict):
            return st
        user = {"id": uid, "is_bot": False, "first_name": f"U{uid}"}
        base = {"status": st, "user": user}
        if st == "creator":
            base.update(is_anonymous=False)
        elif st == "administrator":
            base.update({k: True for k in (
                "can_be_edited", "can_manage_chat", "can_delete_messages", "can_manage_video_chats",
                "can_restrict_members", "can_promote_members", "can_change_info", "can_invite_users",
                "can_post_stories", "can_edit_stories", "can_delete_stories")}, is_anonymous=False)
        elif st in ("restricted", "restricted_left"):
            base = {"status": "restricted", "user": user, "is_member": st == "restricted", "until_date": 0,
                    **{k: False for k in (
                        "can_change_info", "can_invite_users", "can_pin_messages", "can_send_messages",
                        "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
                        "can_manage_topics", "can_send_audios", "can_send_documents", "can_send_photos",
                        "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_edit_tag",
                        "can_react_to_messages")}}
        elif st == "kicked":
            base.update(until_date=0)
        return base

    # -- helpers for assertions -----------------------------------------------------
    def sent(self, api: str, chat_id: Optional[int] = None) -> list[dict]:
        return [p for a, p in self.calls if a == api and (chat_id is None or int(p.get("chat_id", 0)) == chat_id)]

    def texts(self, chat_id: Optional[int] = None) -> list[str]:
        out = []
        for a, p in self.calls:
            if a in ("sendMessage", "editMessageText") and (chat_id is None or int(p.get("chat_id", 0)) == chat_id):
                out.append(p.get("text", ""))
        return out

    def last_poll(self, chat_id: int) -> tuple[str, dict]:
        items = [(pid, p) for pid, p in self.polls.items() if p["chat_id"] == chat_id]
        return items[-1]

    def poll_by_message(self, message_id: int) -> tuple[str, dict]:
        return next((pid, p) for pid, p in self.polls.items() if p["message_id"] == message_id)

    def reset_calls(self):
        self.calls.clear()


class _ApiError(Exception):
    def __init__(self, code: int, description: str):
        self.code, self.description = code, description


# ------------------------------------------------------------ update factory
class UpdateFactory:
    def __init__(self, bot):
        self.bot = bot
        self._uid = itertools.count(1)
        self._mid = itertools.count(1)

    def user(self, uid: int, name: str = "Tester", username: Optional[str] = None) -> dict:
        return {"id": uid, "is_bot": False, "first_name": name, "username": username or f"user{uid}"}

    def _chat(self, chat_id: int) -> dict:
        if chat_id < 0:
            return {"id": chat_id, "type": "supergroup", "title": f"Test Group {-chat_id}"}
        return {"id": chat_id, "type": "private", "first_name": "Tester"}

    def message(self, user: dict, text: Optional[str] = None, chat_id: Optional[int] = None, **extra) -> Update:
        chat_id = chat_id if chat_id is not None else user["id"]
        m = {"message_id": next(self._mid), "date": int(time.time()), "chat": self._chat(chat_id), "from": user}
        if text is not None:
            m["text"] = text
            if text.startswith("/"):
                cmd = text.split()[0]
                m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        m.update(extra)
        return Update.de_json({"update_id": next(self._uid), "message": m}, self.bot)

    def document(self, user: dict, file_id: str, file_name: str, mime: str, size: int, caption=None) -> Update:
        extra = {"document": {"file_id": file_id, "file_unique_id": "u" + file_id, "file_name": file_name,
                              "mime_type": mime, "file_size": size}}
        if caption:
            extra["caption"] = caption
        return self.message(user, None, **extra)

    def photo(self, user: dict, file_id: str, caption=None) -> Update:
        extra = {"photo": [{"file_id": file_id, "file_unique_id": "u" + file_id, "width": 100, "height": 100}]}
        if caption:
            extra["caption"] = caption
        return self.message(user, None, **extra)

    def callback(self, user: dict, data: str, chat_id: Optional[int] = None, message_id: int = 1,
                 message_text: str = "menu") -> Update:
        chat_id = chat_id if chat_id is not None else user["id"]
        cq = {"id": str(next(self._uid)), "from": user, "chat_instance": "ci", "data": data,
              "message": {"message_id": message_id, "date": int(time.time()), "chat": self._chat(chat_id),
                          "from": BOT_USER, "text": message_text}}
        return Update.de_json({"update_id": next(self._uid), "callback_query": cq}, self.bot)

    def user_poll(self, user: dict, question: str, options: list[str], *, quiz: bool = True,
                  correct: Optional[int] = None, explanation: Optional[str] = None,
                  multiple: bool = False, correct_ids: Optional[list[int]] = None,
                  forward_from_chat: Optional[str] = None, legacy_scalar: bool = False) -> Update:
        """A poll message the user created with the native poll creator
        (KeyboardButtonPollType) — Telegram includes correct_option_id and
        explanation for quiz polls created in the bot's private chat."""
        pid = f"user-{next(self._uid)}"
        poll = {"id": pid, "question": question,
                "options": [{"text": o, "voter_count": 0, "persistent_id": f"{pid}-{i}"}
                            for i, o in enumerate(options)],
                "total_voter_count": 0, "is_closed": False, "is_anonymous": True,
                "type": "quiz" if quiz else "regular", "allows_multiple_answers": multiple,
                "allows_revoting": False, "members_only": False}
        if quiz and correct_ids is None and correct is not None:
            correct_ids = [correct]
        if quiz and correct_ids is not None:
            if legacy_scalar:
                poll["correct_option_id"] = correct_ids[0]
            else:
                poll["correct_option_ids"] = correct_ids
        if quiz and explanation:
            poll["explanation"] = explanation
        extra = {"poll": poll}
        if forward_from_chat:
            extra["forward_origin"] = self.channel_origin(forward_from_chat)
        return self.message(user, None, **extra)

    _origin_ids = itertools.count(700)

    def channel_origin(self, username: str) -> dict:
        return {"type": "channel", "date": int(time.time()), "message_id": next(self._origin_ids),
                "chat": {"id": -100777, "type": "channel", "title": username, "username": username}}

    def forwarded_text(self, user: dict, text: str, channel: str = "mcq_channel") -> Update:
        return self.message(user, text, forward_origin=self.channel_origin(channel))

    def chat_member(self, chat: dict, user: dict, old: str, new: str) -> Update:
        return Update.de_json({"update_id": next(self._uid), "chat_member": {
            "chat": chat, "from": user, "date": int(time.time()),
            "old_chat_member": {"status": old, "user": user},
            "new_chat_member": {"status": new, "user": user, **({"until_date": 0} if new == "kicked" else {})}}},
            self.bot)

    def inline_query(self, user: dict, query: str) -> Update:
        return Update.de_json({"update_id": next(self._uid), "inline_query": {
            "id": str(next(self._uid)), "from": user, "query": query, "offset": ""}}, self.bot)

    def poll_answer(self, user: dict, poll_id: str, option: int) -> Update:
        return Update.de_json({"update_id": next(self._uid), "poll_answer": {
            "poll_id": poll_id, "user": user, "option_ids": [option] if option is not None else [],
            "option_persistent_ids": [f"{poll_id}-{option}"] if option is not None else []}}, self.bot)
