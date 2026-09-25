"""
PDF/Text -> Quiz parser.

Important:
- Never guesses the correct answer.
- Supports common MCQ layouts:
    1. Question
       A. option
       B. option
       C. option
       D. option
       Answer: B
- Also supports answer keys such as:
    1-A, 2-C, 3-B
    1. A  2. C  3. B
    1) B
- The answer key must be present in the same source text.
- Scanned/image-only PDFs are not silently OCR'd here.
"""

import re
import json
from pathlib import Path
import fitz


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\r", "\n")).strip()


def extract_pdf_text(pdf_path):
    doc = fitz.open(str(pdf_path))
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    doc.close()
    return "\n".join(pages)


def normalize_lines(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return lines


def extract_answer_key(text):
    """
    Returns {question_number: option_index}.
    It looks for explicit answer-key patterns only.
    """
    answers = {}

    # Examples: 1-A, 2-C / 1. A 2. C / 1) B
    pattern = re.compile(
        r"(?<!\w)(\d{1,3})\s*[\.\)\-:]\s*[\(\[]?([ABCD])[\)\]]?",
        re.I
    )

    for m in pattern.finditer(text):
        n = int(m.group(1))
        letter = m.group(2).upper()
        if 1 <= n <= 1000:
            answers[n] = ord(letter) - 65

    # Examples: Answer: B / Ans B / उत्तर: C
    # These are associated later with question order.
    inline = re.findall(
        r"(?:answer|ans|उत्तर|सही\s*उत्तर)\s*[:\-]?\s*\(?([ABCD])\)?",
        text,
        flags=re.I
    )

    return answers, [ord(x.upper()) - 65 for x in inline]


def parse_questions(text):
    """
    Parse numbered questions with four options.
    Returns questions without assigning guessed answers.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Question starts: 1. / 1) / प्रश्न 1.
    starts = list(re.finditer(
        r"(?m)^\s*(?:प्रश्न\s*)?(\d{1,3})\s*[\.\)]\s+",
        text,
        flags=re.I
    ))

    blocks = []
    for i, m in enumerate(starts):
        start = m.start()
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        blocks.append((int(m.group(1)), text[m.end():end].strip()))

    questions = []

    for number, block in blocks:
        # Find A/B/C/D option labels.
        opt_matches = list(re.finditer(
            r"(?im)(?:^|\n)\s*\(?([ABCD])\)?\s*[\.\:\)]\s*",
            block
        ))

        if len(opt_matches) < 4:
            # Some PDFs place all options on one line.
            opt_matches = list(re.finditer(
                r"(?i)(?:^|\s)\(?([ABCD])\)?\s*[\.\:\)]\s*",
                block
            ))

        if len(opt_matches) < 4:
            continue

        opt_matches = opt_matches[:4]

        question_text = clean(block[:opt_matches[0].start()])

        options = []
        for j, om in enumerate(opt_matches):
            start = om.end()
            end = opt_matches[j + 1].start() if j + 1 < len(opt_matches) else len(block)
            value = clean(block[start:end])

            # Remove trailing answer-key text from option D if present.
            value = re.split(
                r"\b(?:answer|ans|उत्तर|सही\s*उत्तर)\s*[:\-]",
                value,
                flags=re.I
            )[0].strip()

            options.append(value)

        if question_text and len(options) == 4 and all(options):
            questions.append({
                "number": number,
                "question": question_text,
                "options": options
            })

    return questions


def parse_text_to_questions(text):
    """
    Convert supplied text into validated quiz questions.
    """
    if not text or not text.strip():
        return []

    explicit_answers, inline_answers = extract_answer_key(text)
    parsed = parse_questions(text)

    if not parsed:
        return []

    # First use numbered answer-key entries.
    for q in parsed:
        if q["number"] in explicit_answers:
            q["correct"] = explicit_answers[q["number"]]

    # If there is no numbered key, use inline answers in question order.
    if sum("correct" in q for q in parsed) != len(parsed):
        if len(inline_answers) >= len(parsed):
            for i, q in enumerate(parsed):
                q["correct"] = inline_answers[i]

    # Never guess. Every question must have an explicit answer.
    final = []
    for q in parsed:
        if "correct" not in q:
            continue
        if not 0 <= q["correct"] <= 3:
            continue

        final.append({
            "question": q["question"],
            "options": q["options"],
            "correct": q["correct"]
        })

    return final[:100]


def parse_pdf(pdf_path):
    text = extract_pdf_text(pdf_path)
    return parse_text_to_questions(text)


def parse_txt(txt_path):
    text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
    return parse_text_to_questions(text)


def save_questions(questions, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(
        json.dumps(questions[:100], ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
