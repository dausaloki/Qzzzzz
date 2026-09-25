"""
Robust MCQ source parser.

Key rule:
- Question section and answer-key section are parsed separately.
- Numbered entries in the answer key are NEVER treated as new questions.
- The original question order is preserved.
- All parsed questions from one source remain one quiz (up to 100).
- Correct answers are never guessed.
"""

import re
import json
from pathlib import Path
import fitz


ANSWER_HEADING_RE = re.compile(
    r"(?im)^\s*(?:उत्तर\s*(?:माला|कुंजी|तालिका|key)?|"
    r"answer\s*key|answers?|ans(?:wer)?\s*key)\s*[:\-]?\s*$"
)


def clean(text):
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def extract_pdf_text(pdf_path):
    doc = fitz.open(str(pdf_path))
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(pages)


def split_answer_key(text):
    """
    Split source at an explicit answer-key heading.
    If no heading exists, keep all text as question text and try
    explicit inline answers later.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for i, line in enumerate(lines):
        if ANSWER_HEADING_RE.match(line.strip()):
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])

    # Common inline headings such as "उत्तर कुंजी : 1-A 2-B..."
    m = re.search(
        r"(?im)^\s*(?:उत्तर\s*(?:माला|कुंजी|तालिका)|answer\s*key|answers?)\s*[:\-]\s*",
        text
    )
    if m:
        return text[:m.start()], text[m.end():]

    return text, ""


def parse_answer_key(answer_text):
    """
    Supports:
      1-A, 2-B, 3-C
      1. A  2. B
      1) A
      1 A
      प्रश्न 1 - B
    Returns {question_number: zero_based_option_index}
    """
    answers = {}

    patterns = [
        re.compile(
            r"(?<!\w)(\d{1,3})\s*[\.\)\-:]\s*\(?([ABCD])\)?",
            re.I
        ),
        re.compile(
            r"(?<!\w)(\d{1,3})\s+\(?([ABCD])\)?(?!\w)",
            re.I
        ),
        re.compile(
            r"(?i)(?:प्रश्न\s*)?(\d{1,3})\s*(?:का|की|के)?\s*उत्तर\s*[:\-]?\s*\(?([ABCD])\)?"
        )
    ]

    for pattern in patterns:
        for match in pattern.finditer(answer_text):
            number = int(match.group(1))
            letter = match.group(2).upper()
            if 1 <= number <= 1000:
                answers[number] = ord(letter) - 65

    # Also support a plain sequence:
    # उत्तर: B C A D ...
    if not answers:
        seq = re.findall(
            r"\b([ABCD])\b",
            answer_text,
            flags=re.I
        )
        # A plain sequence is accepted only if it is clearly an answer list.
        # Caller will map it only when its length equals question count.
        return {}, [ord(x.upper()) - 65 for x in seq]

    return answers, []


def parse_question_blocks(question_text):
    """
    Parse numbered MCQs ONLY from the question section.
    Because answer-key text has already been removed, its numbers cannot
    become fake questions.
    """
    text = question_text.replace("\r\n", "\n").replace("\r", "\n")

    starts = list(re.finditer(
        r"(?m)^\s*(?:प्रश्न\s*)?(\d{1,3})\s*[\.\)]\s+",
        text
    ))

    blocks = []

    for i, match in enumerate(starts):
        number = int(match.group(1))
        start = match.end()
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        blocks.append((number, text[start:end].strip()))

    questions = []

    for number, block in blocks:
        # Match A/B/C/D at the beginning of a line.
        option_matches = list(re.finditer(
            r"(?im)^\s*\(?([ABCD])\)?\s*[\.\:\)]\s*",
            block
        ))

        if len(option_matches) < 4:
            # Fallback for PDFs that put options on one line.
            option_matches = list(re.finditer(
                r"(?i)(?:^|\s)\(?([ABCD])\)?\s*[\.\:\)]\s*",
                block
            ))

        # Need exactly the first four option labels.
        if len(option_matches) < 4:
            continue

        option_matches = option_matches[:4]

        question = clean(block[:option_matches[0].start()])

        options = []

        for j, match in enumerate(option_matches):
            start = match.end()
            end = (
                option_matches[j + 1].start()
                if j + 1 < len(option_matches)
                else len(block)
            )

            option = clean(block[start:end])

            # Remove inline answer markers from the end of the block.
            option = re.split(
                r"(?i)\b(?:उत्तर|answer|ans)\s*[:\-]",
                option
            )[0].strip()

            options.append(option)

        if (
            question
            and len(options) == 4
            and all(options)
        ):
            questions.append({
                "number": number,
                "question": question,
                "options": options
            })

    # Preserve original order exactly.
    return questions


def parse_text_to_questions(text):
    if not text or not text.strip():
        return []

    question_text, answer_text = split_answer_key(text)

    parsed = parse_question_blocks(question_text)

    if not parsed:
        return []

    numbered_answers, sequence_answers = parse_answer_key(answer_text)

    # If answer key is a plain sequence, only use it when its length
    # exactly matches the number of parsed questions.
    if sequence_answers and len(sequence_answers) == len(parsed):
        for i, q in enumerate(parsed):
            q["correct"] = sequence_answers[i]

    # Otherwise use numbered answer key.
    for q in parsed:
        if q["number"] in numbered_answers:
            q["correct"] = numbered_answers[q["number"]]

    # NEVER guess. Drop questions without an explicit answer.
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

    # If PDF text extraction is almost empty, don't pretend it worked.
    if len(re.sub(r"\s+", "", text)) < 30:
        return []

    return parse_text_to_questions(text)


def parse_txt(txt_path):
    text = Path(txt_path).read_text(
        encoding="utf-8",
        errors="replace"
    )
    return parse_text_to_questions(text)


def save_questions(questions, output_path):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            questions[:100],
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
        )
            
