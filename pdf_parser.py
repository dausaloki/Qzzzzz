import re
import json
from pathlib import Path

# ============================================================
# ROBUST PDF/TEXT MCQ PARSER
# Supports:
# - प्रश्न 1. / 1. / 1) / Q1 / Q.1
# - (A) (B) (C) (D), A. B. C. D., A)...
# - lowercase (a)-(d) used as question data
# - कथन आधारित
# - कथन/कारण
# - सुमेलित / सूची-I / सूची-II
# - कूट:
# - ordering/ranking
# - separate answer key
# - answer immediately after question
# - multiline questions/options
# - page-breaks inside questions
#
# IMPORTANT:
# The parser does NOT treat every A/B/C/D-looking line as an
# answer option. It uses context and a final A-D sequence.
# ============================================================

QUESTION_START = re.compile(
    r"""(?im)^\s*
    (?:
        प्रश्न\s*
        |Q(?:uestion)?\s*
    )?
    (\d{1,3})
    \s*[\.\):\-]\s+
    """,
    re.X,
)

# Also accepts "प्रश्न 1" when there is no punctuation.
QUESTION_START_LOOSE = re.compile(
    r"(?im)^\s*(?:प्रश्न\s+|Q(?:uestion)?\s*)?(\d{1,3})\s*$"
)

ANSWER_KEY_HEADING = re.compile(
    r"""(?im)^\s*
    (?:
        उत्तर\s*कुंजी |
        उत्तरमाला |
        उत्तर\s*तालिका |
        answer\s*key |
        answer\s*sheet |
        answers?
    )
    \s*:?\s*$
    """,
    re.X,
)

ANSWER_LINE = re.compile(
    r"""(?im)^\s*
    (?:
        उत्तर |
        सही\s*उत्तर |
        ans(?:wer)?
    )
    \s*[:\-]\s*
    \(?\s*([ABCD])\s*\)?
    (?:\s+|$)
    """
)

# A-D markers. The content may continue on later lines.
OPTION_MARK = re.compile(
    r"""^\s*
    (?:
        \(([ABCDabcd])\)
        |
        ([ABCDabcd])[\.\):\-]
    )
    \s*(.*)$"""
)

# Lowercase data markers, deliberately separate from uppercase answer options.
LOWER_DATA_MARK = re.compile(
    r"^\s*\(([abcd])\)\s*(.*)$"
)

# Numeric list items such as (1), (2), (3), (4)
NUMERIC_ITEM = re.compile(r"^\s*\((\d+)\)\s*(.*)$")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("\ufeff", "")
    # Keep line structure; do NOT collapse meaningful whitespace.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def is_question_start(line: str):
    m = QUESTION_START.match(line)
    if m:
        return int(m.group(1))
    m = QUESTION_START_LOOSE.match(line)
    if m:
        return int(m.group(1))
    return None


def split_answer_key(text):
    """Separate a final answer-key section from question text."""
    lines = text.splitlines()
    key_start = None

    # Prefer a heading that occurs after the first question.
    for i, line in enumerate(lines):
        if ANSWER_KEY_HEADING.match(line.strip()):
            if i > 0:
                key_start = i
                break

    if key_start is None:
        return text, {}

    body = "\n".join(lines[:key_start]).strip()
    key_text = "\n".join(lines[key_start + 1:]).strip()

    answers = {}
    for line in key_text.splitlines():
        # Examples:
        # 1. A
        # 1) (A)
        # प्रश्न 1 - (A)
        m = re.match(
            r"""^\s*
            (?:प्रश्न\s*|Q(?:uestion)?\s*)?
            (\d{1,3})
            \s*[\.\):\-]\s*
            \(?\s*([ABCDabcd])\s*\)?
            """,
            line,
            re.X | re.I,
        )
        if m:
            answers[int(m.group(1))] = m.group(2).upper()
            continue

        # Compact answer key: 1-A, 2-B, 3-C
        for m in re.finditer(
            r"(?<!\d)(\d{1,3})\s*[-:]\s*\(?([ABCDabcd])\)?",
            line,
        ):
            answers[int(m.group(1))] = m.group(2).upper()

    return body, answers


def find_question_blocks(text):
    """Split source into question blocks without splitting inside a question."""
    lines = text.splitlines()
    starts = []

    for i, line in enumerate(lines):
        n = is_question_start(line)
        if n is not None:
            starts.append((i, n))

    # Remove duplicate start on the same line/location.
    unique = []
    seen = set()
    for item in starts:
        if item[0] not in seen:
            unique.append(item)
            seen.add(item[0])

    blocks = []
    for idx, (start, number) in enumerate(unique):
        end = unique[idx + 1][0] if idx + 1 < len(unique) else len(lines)
        raw = "\n".join(lines[start:end]).strip()
        blocks.append((number, raw))

    return blocks


def extract_inline_answer(raw):
    # Search all explicit answer lines and use the last one in the block.
    matches = list(ANSWER_LINE.finditer(raw))
    if matches:
        return matches[-1].group(1).upper()

    # More tolerant answer phrases.
    patterns = [
        r"(?im)^\s*उत्तर\s*\(?\s*([ABCD])\s*\)?\s*$",
        r"(?im)^\s*सही\s*उत्तर\s*[:\-]?\s*\(?\s*([ABCD])\s*\)?",
        r"(?im)^\s*ans(?:wer)?\s*[:\-]?\s*\(?\s*([ABCD])\s*\)?",
    ]
    for pattern in patterns:
        matches = list(re.finditer(pattern, raw))
        if matches:
            return matches[-1].group(1).upper()

    return None


def remove_answer_lines(lines):
    result = []
    for line in lines:
        if ANSWER_LINE.match(line):
            continue
        if re.match(
            r"(?im)^\s*(?:सही\s*उत्तर|उत्तर|ans(?:wer)?)\s*\(?\s*[ABCDabcd]\s*\)?\s*$",
            line,
        ):
            continue
        result.append(line)
    return result


def strip_question_prefix(first_line):
    return re.sub(
        r"""^\s*
        (?:
            प्रश्न\s*|
            Q(?:uestion)?\s*
        )?
        \d{1,3}
        \s*[\.\):\-]\s*
        """,
        "",
        first_line,
        flags=re.I | re.X,
    )


def is_strong_answer_cue(line):
    low = line.lower().strip()
    cues = (
        "कूट:",
        "कूट :",
        "नीचे दिए गए विकल्पों",
        "सबसे उपयुक्त उत्तर",
        "सही कथनों का चयन",
        "उपयुक्त उत्तर चुनें",
        "सही विकल्प चुनें",
        "निम्नलिखित में से सही",
        "choose the correct",
        "select the correct",
        "following options",
        "code:",
    )
    return any(cue.lower() in low for cue in cues)


def marker_info(line):
    m = OPTION_MARK.match(line)
    if not m:
        return None
    label = (m.group(1) or m.group(2)).upper()
    text = m.group(3).strip()
    return label, text


def find_all_upper_option_runs(lines):
    """
    Find runs A,B,C,D. A run can have multiline option bodies.
    Return candidate start positions and confidence.
    """
    candidates = []

    for i, line in enumerate(lines):
        info = marker_info(line)
        if not info or info[0] != "A":
            continue

        labels = []
        positions = []
        j = i
        current_label = None

        while j < len(lines) and len(labels) < 4:
            mi = marker_info(lines[j])
            if mi:
                lab = mi[0]
                if not labels:
                    if lab != "A":
                        break
                else:
                    expected = "ABCD"[len(labels)]
                    if lab != expected:
                        break
                labels.append(lab)
                positions.append(j)
            j += 1

        if labels == list("ABCD"):
            # More confidence if each marker appears on a separate line.
            candidates.append((i, positions, 0))

    return candidates


def find_option_start(lines):
    """
    Context-first detection.

    Priority:
    1. A-D run after strong cue such as कूट: or "नीचे दिए गए विकल्प..."
    2. Last A-D run in the block, because question data often contains
       lowercase a-d or an earlier uppercase list.
    3. A-D run with the highest contextual confidence.
    """
    runs = find_all_upper_option_runs(lines)
    if not runs:
        return None

    # 1) Strong cue followed by A-D.
    for cue_i, line in enumerate(lines):
        if not is_strong_answer_cue(line):
            continue
        for start, positions, _ in runs:
            if start > cue_i:
                return start

    # 2) In formats like:
    # (a) data...
    # ...
    # (A) real option...
    # the last A-D run is normally the answer set.
    return runs[-1][0]


def parse_options(lines, start):
    """
    Parse A-D from start. Continuation lines are preserved.
    Stops after D unless an obvious next section begins.
    """
    options = {}
    current = None

    for idx in range(start, len(lines)):
        line = lines[idx]
        mi = marker_info(line)

        if mi:
            label, content = mi
            if label in "ABCD" and label not in options:
                current = label
                options[label] = content
                continue

            # If a repeated A-D starts after all four options, stop.
            if label in options and set(options) == set("ABCD"):
                break

        if current:
            # Keep every continuation line. Do not truncate long options.
            stripped = line.strip()
            if stripped:
                options[current] += "\n" + stripped

    if set(options) != set("ABCD"):
        return None

    return options


def clean_question_text(lines, option_start):
    qlines = lines[:option_start]

    # Remove blank lines only at the edges.
    while qlines and not qlines[0].strip():
        qlines.pop(0)
    while qlines and not qlines[-1].strip():
        qlines.pop()

    # Preserve the source wording and line order.
    return "\n".join(qlines).strip()


def parse_block(number, raw, separate_answer=None):
    inline_answer = extract_inline_answer(raw)
    answer = inline_answer or separate_answer

    raw_lines = raw.splitlines()
    lines = remove_answer_lines(raw_lines)

    if not lines:
        return None, "empty question"

    lines[0] = strip_question_prefix(lines[0]).strip()

    option_start = find_option_start(lines)
    if option_start is None:
        return None, "options A-D not found"

    options = parse_options(lines, option_start)
    if options is None:
        return None, "all four final options A-D not found"

    question = clean_question_text(lines, option_start)

    if not question:
        return None, "question text is empty"

    if answer not in "ABCD":
        return None, "explicit answer not found"

    return {
        "id": f"q{number}",
        "number": number,
        "question": question,
        "options": [
            {"label": "A", "text": options["A"]},
            {"label": "B", "text": options["B"]},
            {"label": "C", "text": options["C"]},
            {"label": "D", "text": options["D"]},
        ],
        "correct": answer,
    }, None


def parse_source_text(text, max_questions=100):
    text = normalize_text(text)
    body, answer_key = split_answer_key(text)

    blocks = find_question_blocks(body)
    if not blocks:
        raise ValueError("कोई प्रश्न क्रमांक नहीं मिला।")

    parsed = []
    errors = []

    for number, raw in blocks:
        q, err = parse_block(number, raw, answer_key.get(number))
        if q is not None:
            parsed.append(q)
        else:
            errors.append(f"प्रश्न {number}: {err}")

    # IMPORTANT: never silently create a partial question bank.
    if errors:
        raise ValueError(
            "कुछ प्रश्न पूरी तरह parse नहीं हुए:\n" +
            "\n".join(errors)
        )

    # Preserve source order, not alphabetical/dictionary order.
    parsed.sort(key=lambda x: x["number"])

    if len(parsed) > max_questions:
        parsed = parsed[:max_questions]

    # Duplicate numbers are an integrity error.
    nums = [q["number"] for q in parsed]
    if len(nums) != len(set(nums)):
        raise ValueError("Duplicate question number मिला।")

    return parsed


def extract_pdf_text(pdf_path):
    import fitz

    doc = fitz.open(pdf_path)
    pages = []

    for page in doc:
        pages.append(page.get_text("text"))

    text = "\n".join(pages)

    if not text.strip():
        raise ValueError(
            "PDF में selectable text नहीं मिला। "
            "यह scanned/image PDF हो सकता है; OCR की जरूरत है।"
        )

    return text


def parse_pdf(pdf_path, max_questions=100):
    return parse_source_text(
        extract_pdf_text(pdf_path),
        max_questions=max_questions
    )


def save_questions(questions, path="questions.json"):
    Path(path).write_text(
        json.dumps(questions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
