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
BOT_USER = {"id": 999000, "is_bot": True, "first_name": "Quiz Bot", "username": "TestQuizBot"}



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
        if p.get("type") == "quiz":
            cid = p.get("correct_option_id")
            if cid is None or not 0 <= int(cid) < len(texts):
                raise _ApiError(400, "Bad Request: wrong correct option id")
        op = p.get("open_period")
        if op is not None and not 5 <= int(op) <= 600:
            raise _ApiError(400, "Bad Request: open_period must be 5..600")
        pid = str(next(self._poll_ids))
        msg = self._message(p["chat_id"])
        self.polls[pid] = {"question": q, "options": texts, "correct_option_id": p.get("correct_option_id"),
                           "chat_id": int(p["chat_id"]), "message_id": msg["message_id"], "is_closed": False,
                           "explanation": expl, "open_period": op, "reply_markup": p.get("reply_markup")}
        msg["poll"] = self._poll_obj(pid)
        return msg

    def _poll_obj(self, pid: str) -> dict:
        poll = self.polls[pid]
        return {"id": pid, "question": poll["question"],
                "options": [{"text": t, "voter_count": 0} for t in poll["options"]],
                "total_voter_count": 0, "is_closed": poll["is_closed"], "is_anonymous": False,
                "type": "quiz", "allows_multiple_answers": False,
                "correct_option_id": poll["correct_option_id"]}

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
            return {"id": chat_id, "type": "supergroup", "title": "Test Group"}
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

    def poll_answer(self, user: dict, poll_id: str, option: int) -> Update:
        return Update.de_json({"update_id": next(self._uid), "poll_answer": {
            "poll_id": poll_id, "user": user, "option_ids": [option]}}, self.bot)
