import re
import json
from pathlib import Path

QUESTION_START = re.compile(
    r'(?im)^\s*(?:प्रश्न\s*)?(\d{1,3})\s*[\.\):\-]\s*'
)

ANSWER_KEY_HEADING = re.compile(
    r'(?im)^\s*(?:उत्तर\s*कुंजी|उत्तरमाला|answer\s*key)\s*:?\s*$'
)

ANSWER_LINE = re.compile(r'(?im)^\s*उत्तर\s*:\s*\(?([ABCD])\)?(?:\s|$)')

OPTION_LINE = re.compile(
    r'^\s*(?:\(([ABCD])\)|([ABCD])[\.\):])\s*(.+?)\s*$',
    re.I
)

def normalize_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def split_answer_key(text):
    lines = text.splitlines()
    key_start = None
    for i, line in enumerate(lines):
        if ANSWER_KEY_HEADING.match(line.strip()):
            key_start = i
            break
    if key_start is None:
        return text, {}
    body = '\n'.join(lines[:key_start])
    key_text = '\n'.join(lines[key_start + 1:])
    answers = {}
    for m in re.finditer(r'(?im)(?:प्रश्न\s*)?(\d{1,3})\s*[\.\):\-]\s*\(?([ABCD])\)?', key_text):
        answers[int(m.group(1))] = m.group(2).upper()
    return body, answers

def find_question_blocks(text):
    matches = list(QUESTION_START.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        number = int(m.group(1))
        raw = text[start:end].strip()
        blocks.append((number, raw))
    return blocks

def extract_answer(raw):
    matches = list(ANSWER_LINE.finditer(raw))
    if matches:
        return matches[-1].group(1).upper()
    # Common compact forms: Ans: A / Answer: (A)
    m = re.search(r'(?im)^\s*(?:ans(?:wer)?|सही\s*उत्तर)\s*[:\-]\s*\(?([ABCD])\)?\b', raw)
    return m.group(1).upper() if m else None

def remove_answer_line(raw):
    raw = re.sub(r'(?im)^\s*(?:उत्तर|सही\s*उत्तर|ans(?:wer)?)\s*[:\-].*$', '', raw)
    return raw.strip()

def option_candidates(lines):
    return [(i, OPTION_LINE.match(line)) for i, line in enumerate(lines) if OPTION_LINE.match(line)]

def choose_option_start(lines):
    # Strong contextual cues used by the formats supplied by the user.
    cues = [
        r'कूट\s*:',
        r'नीचे दिए गए विकल्पों',
        r'सही कथनों का चयन',
        r'उपयुक्त उत्तर चुनें',
        r'सबसे उपयुक्त उत्तर',
    ]
    for i, line in enumerate(lines):
        if any(re.search(c, line, re.I) for c in cues):
            for j in range(i + 1, len(lines)):
                if OPTION_LINE.match(lines[j]):
                    return j
    # For simple MCQs, use a run of A-D only when no earlier list/matching
    # data labels are present.
    opts = option_candidates(lines)
    for pos, _ in opts:
        labels = []
        for _, m in opts:
            if _ >= pos and _ < pos + 12:
                labels.append(m.group(1) or m.group(2))
        if set(x.upper() for x in labels[:4]) == {"A","B","C","D"}:
            return pos
    return None

def parse_block(number, raw, separate_key=None):
    answer = extract_answer(raw) or separate_key
    clean = remove_answer_line(raw)
    lines = [x.rstrip() for x in clean.splitlines()]
    if not lines:
        return None, "empty question"

    # Remove the question-number prefix only; preserve the remaining wording.
    first = re.sub(
        r'^\s*(?:प्रश्न\s*)?\d{1,3}\s*[\.\):\-]\s*',
        '',
        lines[0],
        flags=re.I
    )
    lines[0] = first

    opt_start = choose_option_start(lines)
    if opt_start is None:
        return None, "options A-D not found"

    # If the cue itself is "कूट:", it belongs to the question body.
    question_lines = lines[:opt_start]
    option_lines = lines[opt_start:]

    # Collect exactly the first A-D option blocks. Continuation lines belong
    # to the preceding option and are preserved verbatim.
    options = {}
    current = None
    for line in option_lines:
        m = OPTION_LINE.match(line)
        if m:
            label = (m.group(1) or m.group(2)).upper()
            if label in "ABCD" and label not in options:
                current = label
                options[label] = m.group(3).strip()
                continue
        if current:
            # Do not discard long multiline option text.
            options[current] += "\n" + line.strip()

    if set(options) != set("ABCD"):
        return None, "all four options A-D were not found"

    question = "\n".join(x for x in question_lines).strip()
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
        if q:
            parsed.append(q)
        else:
            errors.append(f"प्रश्न {number}: {err}")

    if errors:
        raise ValueError("कुछ प्रश्न पूरी तरह parse नहीं हुए:\n" + "\n".join(errors))

    parsed.sort(key=lambda x: x["number"])
    if len(parsed) > max_questions:
        parsed = parsed[:max_questions]

    # Do not silently accept duplicate question numbers.
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
        raise ValueError("PDF में selectable text नहीं मिला। यह scanned/image PDF हो सकता है; OCR की जरूरत है।")
    return text

def parse_pdf(pdf_path, max_questions=100):
    return parse_source_text(extract_pdf_text(pdf_path), max_questions=max_questions)

def save_questions(questions, path="questions.json"):
    Path(path).write_text(
        json.dumps(questions, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
