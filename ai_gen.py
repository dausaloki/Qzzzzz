"""Advanced Quiz Creation helpers (Feature 1 & Feature 9).

Provides:
1. OpenRouter AI-assisted MCQ generation (``generate_ai_questions``) with
   configurable ``OPENROUTER_API_KEY`` / ``OPENROUTER_DEFAULT_KEYS`` rotation
   and ``OPENROUTER_MODEL``. Secrets are read exclusively from environment
   variables and never logged.
2. Smart Poll / Question text filtering (``clean_poll_or_question_text``,
   ``apply_smart_filter_to_quiz``) for stripping progress tags like ``[1/100]``,
   promo links, ``@usernames``, or custom watermark words when requested.
3. Structured JSON quiz file import (``parse_json_quiz``) feeding into the
   standard preview -> confirm -> create pipeline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Optional

import config
import database as db
import pdf_parser

log = logging.getLogger("quizbot.ai_gen")

_PROGRESS_TAG_RE = re.compile(
    r"^\s*(?:🎯\s*[^\n]*\n+)?\s*(?:\[\s*\d+\s*/\s*\d+\s*\]|\(\s*\d+\s*/\s*\d+\s*\)|Q\.?\s*\d+\s*/\s*\d+\s*[:.)-]?\s*)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?://|t\.me/)\S+", re.IGNORECASE)
_USERNAME_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,32}\b")


def get_openrouter_keys() -> list[str]:
    """Return configured OpenRouter API keys in priority order (never logged)."""
    keys: list[str] = []
    primary = (os.getenv("OPENROUTER_API_KEY", "") or getattr(config, "OPENROUTER_API_KEY", "")).strip()
    if primary:
        keys.append(primary)
    fallback_raw = (
        os.getenv("OPENROUTER_DEFAULT_KEYS", "") or getattr(config, "OPENROUTER_DEFAULT_KEYS", "")
    ).strip()
    for part in fallback_raw.replace(";", ",").split(","):
        k = part.strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def get_openrouter_model() -> str:
    return (
        os.getenv("OPENROUTER_MODEL", "").strip()
        or getattr(config, "OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
        or "openai/gpt-4o-mini"
    )


def get_openrouter_base_url() -> str:
    return (
        os.getenv("OPENROUTER_BASE_URL", "").strip().rstrip("/")
        or getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
        or "https://openrouter.ai/api/v1"
    )


def is_ai_configured() -> bool:
    return bool(get_openrouter_keys())


def _default_http_post(url: str, headers: dict[str, str], payload: dict, timeout: float = 45.0) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        return int(exc.code), err_body


def _extract_json_array(raw_text: str) -> Optional[list]:
    text = (raw_text or "").strip()
    if not text:
        return None
    # Strip markdown code fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
            return parsed["questions"]
    except Exception:
        pass
    # Find outermost [...]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return None


def _item_number(item, idx: int) -> int:
    """The item's own ``number`` when it is a plain integer, else its position."""
    raw = item.get("number") if isinstance(item, dict) else None
    try:
        return int(str(raw).strip()) if raw not in (None, "") and not isinstance(raw, bool) else idx
    except ValueError:
        return idx


def _normalize_item_with_reason(item, idx: int) -> tuple[Optional[dict], str]:
    """Validate one MCQ item.  Returns ``(question, "")`` or ``(None, reason)``.

    The answer is never guessed: it must be an explicit index / letter / number
    that points at a non-empty option.  It is resolved against the ORIGINAL option
    list first; blank options are dropped afterwards and the index is re-mapped,
    so a blank option can never shift the answer to a different option.
    """
    if not isinstance(item, dict):
        return None, "प्रश्न JSON object नहीं है"
    q_text = str(item.get("question") or item.get("q") or item.get("stem") or "").strip()
    if not q_text:
        return None, "प्रश्न का text खाली है"
    raw_opts = item.get("options") or item.get("choices") or []
    if not isinstance(raw_opts, list):
        return None, "options एक list नहीं है"
    raw = ["" if o is None else str(o).strip() for o in raw_opts]
    ci = item.get("correct_index")
    if ci is None:
        ci = item.get("answer_index", item.get("correct"))
    if isinstance(ci, str):
        s = ci.strip().upper()
        if s in ("A", "B", "C", "D", "E", "F"):
            ci = ord(s) - ord("A")
        elif s in ("अ", "ब", "स", "द"):
            ci = {"अ": 0, "ब": 1, "स": 2, "द": 3}[s]
        elif s.isdigit():
            val = int(s)
            ci = val - 1 if 1 <= val <= len(raw) else val
    if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= ci < len(raw)):
        return None, "सही उत्तर (correct_index / answer_index / correct) नहीं मिला या सीमा से बाहर है — अनुमान नहीं लगाया गया"
    if not raw[ci]:
        return None, "सही उत्तर वाला option खाली है — अनुमान नहीं लगाया गया"
    keep = [i for i, o in enumerate(raw) if o]
    options = [raw[i] for i in keep]
    if not (config.MIN_OPTIONS <= len(options) <= config.MAX_OPTIONS):
        return None, f"{len(options)} option(s) — {config.MIN_OPTIONS}–{config.MAX_OPTIONS} चाहिए"
    expl = str(item.get("explanation") or item.get("solution") or "").strip()
    section = str(item.get("section") or "").strip()
    return {
        "number": _item_number(item, idx),
        "question": q_text,
        "options": options,
        "correct_index": keep.index(ci),
        "explanation": expl,
        "qtype": "mcq",
        "pre_text": str(item.get("pre_text") or "").strip(),
        "section": section,
    }, ""


def _normalize_ai_item(item: dict, idx: int) -> Optional[dict]:
    return _normalize_item_with_reason(item, idx)[0]


def parse_ai_response_questions(content: str) -> list[dict]:
    """Parse AI model output (JSON array or standard MCQ text) into validated questions."""
    arr = _extract_json_array(content)
    questions: list[dict] = []
    if arr is not None:
        for i, item in enumerate(arr, start=1):
            norm = _normalize_ai_item(item, i)
            if norm is not None:
                questions.append(norm)
        if questions:
            return questions

    # Fallback: parse through universal MCQ parser
    res = pdf_parser.parse_source(content or "")
    for i, pq in enumerate(res.questions, start=1):
        if pq.correct_index is not None and 0 <= pq.correct_index < len(pq.options):
            questions.append({
                "number": pq.number or i,
                "question": pq.question,
                "options": list(pq.options),
                "correct_index": int(pq.correct_index),
                "explanation": pq.explanation or "",
                "qtype": pq.qtype or "mcq",
                "pre_text": pq.pre_text or "",
                "section": getattr(pq, "section_label", "") or "",
            })
    return questions


def generate_ai_questions(
    topic: str,
    count: int = 5,
    *,
    language: str = "Hindi",
    difficulty: str = "medium",
    http_post: Optional[Callable] = None,
) -> list[dict]:
    """Generate validated MCQs on ``topic`` using OpenRouter API with key fallback."""
    clean_topic = (topic or "").strip()
    if not clean_topic:
        raise ValueError("कृपया quiz का विषय (topic) लिखें।")
    n = max(1, min(50, int(count or 5)))
    keys = get_openrouter_keys()
    if not keys:
        raise RuntimeError(
            "AI Quiz Generation अभी configure नहीं है (OPENROUTER_API_KEY / OPENROUTER_DEFAULT_KEYS सेट नहीं है)।"
        )

    post_fn = http_post or _default_http_post
    url = f"{get_openrouter_base_url()}/chat/completions"
    model = get_openrouter_model()

    system_prompt = (
        "You are an expert exam question setter for competitive exams. "
        "Return ONLY a valid JSON array of multiple-choice questions. "
        "Each element MUST be an object with exact keys: "
        '"question" (string), "options" (array of 4 distinct non-empty strings), '
        '"correct_index" (integer 0 to 3), and "explanation" (concise explanation string).'
    )
    user_prompt = (
        f"Generate exactly {n} multiple-choice questions on the topic: {clean_topic}\n"
        f"Language: {language}\n"
        f"Difficulty: {difficulty}\n"
        "Output ONLY a JSON array."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
    }

    last_err = "Unknown error"
    for key_idx, api_key in enumerate(keys):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/kalam_kranti",
            "X-Title": "Lokesh Dausa Quiz Bot",
        }
        try:
            status, body = post_fn(url, headers, payload)
        except Exception as exc:
            log.warning("OpenRouter request failed on key #%d: %s", key_idx + 1, type(exc).__name__)
            last_err = "Network error contacting AI service"
            continue

        if status != 200:
            log.warning("OpenRouter key #%d returned HTTP %s", key_idx + 1, status)
            last_err = f"AI service returned HTTP {status}"
            continue

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except Exception:
            last_err = "Invalid response structure from AI service"
            continue

        questions = parse_ai_response_questions(content)
        if questions:
            return questions[:n]
        last_err = "AI response did not contain valid MCQs"

    raise RuntimeError(f"AI से प्रश्न तैयार नहीं हो सके ({last_err})।")


# ------------------------------------------------ Smart Poll / Text Filtering
def clean_poll_or_question_text(
    text: str,
    *,
    strip_progress_tags: bool = True,
    strip_links: bool = False,
    strip_usernames: bool = False,
    custom_words: Optional[list[str]] = None,
) -> str:
    """Clean forwarded poll or question text without altering core wording unless requested."""
    if not text:
        return ""
    out = str(text)
    if out.startswith(config.QUESTION_TAG):
        out = out[len(config.QUESTION_TAG) :].lstrip()
    if strip_progress_tags:
        out = _PROGRESS_TAG_RE.sub("", out, count=1)
    if strip_links:
        out = _URL_RE.sub("", out)
    if strip_usernames:
        out = _USERNAME_RE.sub("", out)
    if custom_words:
        for word in custom_words:
            w = (word or "").strip()
            if w:
                out = re.sub(re.escape(w), "", out, flags=re.IGNORECASE)
    # Collapse excess blank lines/spaces left by stripped tags while keeping line breaks intact
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in out.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln or len(lines) == 1).strip()
    return cleaned or text.strip()


def apply_smart_filter_to_quiz(
    quiz_id: str,
    *,
    strip_progress_tags: bool = True,
    strip_links: bool = True,
    strip_usernames: bool = True,
    custom_words: Optional[list[str]] = None,
) -> dict:
    """Apply smart filter across a quiz's questions and explanations atomically.

    Never saves an empty question text — if cleaning would empty a question, the
    original question text is preserved.
    """
    questions = db.get_questions(quiz_id)
    updated_count = 0
    for q in questions:
        new_q = clean_poll_or_question_text(
            q["question"],
            strip_progress_tags=strip_progress_tags,
            strip_links=strip_links,
            strip_usernames=strip_usernames,
            custom_words=custom_words,
        )
        new_expl = clean_poll_or_question_text(
            q.get("explanation") or "",
            strip_progress_tags=False,
            strip_links=strip_links,
            strip_usernames=strip_usernames,
            custom_words=custom_words,
        )
        if not new_q.strip():
            new_q = q["question"]
        if new_q != q["question"] or new_expl != (q.get("explanation") or ""):
            db.update_question(q["id"], question=new_q, explanation=new_expl)
            updated_count += 1
    return {"total": len(questions), "updated": updated_count}


# --------------------------------------------------------- JSON Quiz Import
def parse_json_quiz_report(raw_text: str) -> tuple[str, list[dict], list[tuple[Optional[int], str]]]:
    """Parse a JSON quiz file into ``(title, valid_questions, rejected)``.

    ``rejected`` lists ``(question number, reason)`` for every item that is not a
    valid MCQ, so the import review can show it -- nothing is dropped silently.
    Extra quizzes in a ``{"quizzes": [...]}`` file (only the first is imported)
    are reported as one ``(None, reason)`` entry.
    Raises ``ValueError`` (with the reasons) when no item is valid.
    """
    try:
        data = json.loads(raw_text)
    except Exception as exc:
        raise ValueError(f"JSON file invalid है: {exc}") from exc

    title = ""
    items: list = []
    file_notes: list[tuple[Optional[int], str]] = []
    if isinstance(data, dict):
        title = str(data.get("title") or data.get("name") or "").strip()
        if isinstance(data.get("questions"), list):
            items = data["questions"]
        elif isinstance(data.get("quizzes"), list) and data["quizzes"]:
            first = data["quizzes"][0]
            if isinstance(first, dict):
                title = title or str(first.get("title") or "").strip()
                items = first.get("questions") or []
            extra = data["quizzes"][1:]
            if extra:
                names = ", ".join(
                    f"'{str(qz.get('title') or '').strip() or f'#{i}'}' "
                    f"({len(qz.get('questions') or []) if isinstance(qz.get('questions'), list) else 0} प्रश्न)"
                    if isinstance(qz, dict) else f"#{i}"
                    for i, qz in enumerate(extra, start=2))
                file_notes.append((None, f"इस file में {len(data['quizzes'])} quizzes हैं; केवल पहली import हुई। "
                                         f"बाकी import नहीं हुईं: {names} — इन्हें अलग .json file में भेजें।"))
    elif isinstance(data, list):
        items = data

    questions: list[dict] = []
    rejected: list[tuple[Optional[int], str]] = []
    for idx, item in enumerate(items if isinstance(items, list) else [], start=1):
        norm, why = _normalize_item_with_reason(item, idx)
        if norm is not None:
            questions.append(norm)
        else:
            rejected.append((_item_number(item, idx), why))
    rejected += file_notes
    if not questions:
        detail = "".join(f"\n• प्रश्न {n}: {why}" if n is not None else f"\n• {why}" for n, why in rejected[:10])
        more = f"\n… और {len(rejected) - 10}" if len(rejected) > 10 else ""
        raise ValueError("JSON file में कोई मान्य MCQ प्रश्न नहीं मिला।" + detail + more)
    return title, questions, rejected


def parse_json_quiz(raw_text: str) -> tuple[str, list[dict]]:
    """Backward-compatible wrapper: ``(title, valid_questions)``.  Use
    :func:`parse_json_quiz_report` to also get the rejected items."""
    title, questions, _rejected = parse_json_quiz_report(raw_text)
    return title, questions
