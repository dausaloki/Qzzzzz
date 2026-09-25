import re
import fitz

QUESTION_RE = re.compile(
    r"(?m)^\s*(?:प्रश्न\s*)?(\d{1,3})\s*[\.\):\-]\s*"
)

def normalize(s):
    s = s.replace("\u00a0", " ").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def extract_pdf_text(path):
    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError(
            "PDF में selectable text नहीं मिला। यह scanned/image PDF हो सकता है; "
            "पहले OCR करके PDF दें।"
        )
    return text

def answer_key_from_text(text):
    keys = {}
    # Explicit answer lines anywhere.
    for m in re.finditer(
        r"(?im)^\s*(?:प्रश्न\s*)?(\d{1,3})\s*[\.\):\-]?\s*"
        r"(?:उत्तर|Answer)\s*[:\-]\s*\(?\s*([A-Da-d])\s*\)?",
        text
    ):
        keys[int(m.group(1))] = m.group(2).upper()
    return keys

def split_question_blocks(text):
    matches = list(QUESTION_RE.finditer(text))
    if not matches:
        raise ValueError("प्रश्न 1., प्रश्न 2. जैसे question markers नहीं मिले।")
    blocks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        blocks.append((int(m.group(1)), text[start:end].strip()))
    return blocks

def clean_answer_line(block):
    # Keep everything except the explicit final answer line.
    block = re.sub(
        r"(?im)^\s*(?:उत्तर|Answer)\s*[:\-].*$", "", block
    )
    return block.strip()

def option_lines(block):
    lines = [x.rstrip() for x in block.splitlines() if x.strip()]
    return lines

def parse_block(num, raw, global_keys):
    block = clean_answer_line(raw)
    lines = option_lines(block)

    # Remove a trailing answer-key explanation line if it survived.
    while lines and re.match(r"^\s*(?:उत्तर|Answer)\s*[:\-]", lines[-1], re.I):
        lines.pop()

    # Find explicit answer option lines after "कूट:" or the common ordering prompt.
    marker_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\bकूट\s*:", line, re.I):
            marker_idx = i
    if marker_idx is None:
        for i, line in enumerate(lines):
            if re.search(r"नीचे दिए गए विकल्पों में से|सबसे उपयुक्त उत्तर चुनें", line):
                # This line itself belongs to question, options begin after it.
                marker_idx = i

    option_pattern = re.compile(r"^\s*\(([A-D])\)\s*(.+?)\s*$")
    candidates = []
    for i, line in enumerate(lines):
        m = option_pattern.match(line)
        if m:
            candidates.append((i, m.group(1), m.group(2)))

    # If a marker exists, use A-D after marker. This avoids treating matching data
    # A-D lines as answer choices.
    chosen = []
    if marker_idx is not None:
        chosen = [x for x in candidates if x[0] > marker_idx]
    else:
        # Group contiguous A-D choices and choose the last complete run.
        groups = []
        cur = []
        for item in candidates:
            if not cur or item[0] == cur[-1][0] + 1:
                cur.append(item)
            else:
                if len(cur) >= 2:
                    groups.append(cur)
                cur = [item]
        if len(cur) >= 2:
            groups.append(cur)
        if groups:
            chosen = max(groups, key=lambda g: g[-1][0])

    if len(chosen) < 4:
        raise ValueError(f"प्रश्न {num}: A-D के 4 options पूरी तरह नहीं मिले।")

    # Only first four unique A-D in the selected run.
    by_letter = {}
    for idx, letter, text in chosen:
        by_letter[letter] = (idx, text)
    if set(by_letter) != set("ABCD"):
        raise ValueError(f"प्रश्न {num}: A-D options complete नहीं हैं।")

    ordered = [by_letter[x][1].strip() for x in "ABCD"]
    first_option_line = min(by_letter[x][0] for x in "ABCD")

    # Question content is everything before the selected answer-choice block.
    q_lines = lines[:first_option_line]

    # In marker-based formats, keep the marker/prompt in the question.
    question = "\n".join(q_lines).strip()

    # For standard questions, q_lines may include the question number and body.
    if not question:
        raise ValueError(f"प्रश्न {num}: question text खाली है।")

    correct_letter = global_keys.get(num)
    if correct_letter is None:
        # Try explicit answer line from raw block.
        m = re.search(r"(?im)\b(?:उत्तर|Answer)\s*[:\-]\s*\(?([A-Da-d])\)?", raw)
        if m:
            correct_letter = m.group(1).upper()
    if correct_letter not in "ABCD":
        raise ValueError(f"प्रश्न {num}: सही उत्तर (A-D) नहीं मिला।")

    explanation = ""
    # Preserve an optional explicit explanation after answer if present.
    em = re.search(r"(?is)(?:व्याख्या|Explanation)\s*[:\-]\s*(.+)$", raw)
    if em:
        explanation = normalize(em.group(1))

    return {
        "number": num,
        "question": question,
        "options": ordered,
        "correct_index": "ABCD".index(correct_letter),
        "explanation": explanation,
    }

def parse_text(text):
    text = text.replace("\ufeff", "")
    keys = answer_key_from_text(text)
    blocks = split_question_blocks(text)

    questions = []
    errors = []
    seen = set()

    for num, raw in blocks:
        if num in seen:
            continue
        seen.add(num)
        try:
            questions.append(parse_block(num, raw, keys))
        except Exception as e:
            errors.append(str(e))

    if errors:
        raise ValueError(
            "कुछ प्रश्न पूरी तरह parse नहीं हुए:\n" + "\n".join(errors)
        )

    questions.sort(key=lambda x: x["number"])
    if len(questions) > 100:
        raise ValueError("एक quiz में अधिकतम 100 प्रश्न रखे जा सकते हैं।")
    return questions

def parse_pdf(path):
    return parse_text(extract_pdf_text(path))
