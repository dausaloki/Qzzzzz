# सुरक्षित PDF parser.
# यह केवल text-based PDFs से स्पष्ट MCQ संरचना निकालता है।
# Scanned/image PDFs के लिए OCR अलग से जोड़ना होगा; bot अनुमान लगाकर प्रश्न नहीं बनाएगा.

import re
from pathlib import Path
import fitz  # PyMuPDF

def parse_pdf(pdf_path):
    doc = fitz.open(str(pdf_path))
    text = "\n".join(page.get_text("text") for page in doc)
    text = text.replace("\r", "\n")

    # Common question numbering: 1. / 1) / Q1.
    pattern = re.compile(
        r"(?ms)(?:^|\n)\s*(\d{1,3})[\.\)]\s*(.*?)"
        r"\n\s*\(?A\)?[\.:\)]\s*(.*?)"
        r"\n\s*\(?B\)?[\.:\)]\s*(.*?)"
        r"\n\s*\(?C\)?[\.:\)]\s*(.*?)"
        r"\n\s*\(?D\)?[\.:\)]\s*(.*?)(?=\n\s*\d{1,3}[\.\)]|\Z)"
    )

    questions = []
    for m in pattern.finditer(text):
        question = " ".join(m.group(2).split())
        options = [" ".join(m.group(i).split()) for i in range(3, 7)]
        # Correct answer is NOT guessed. PDF must contain a recognizable answer key.
        questions.append({
            "question": question,
            "options": options,
            "correct": 0,
            "_needs_answer_key": True
        })

    # Do not silently guess correct answers.
    # Only return questions if a separate supported answer key is found.
    answer_matches = re.findall(
        r"(?:Answer|Ans|उत्तर)\s*[:\-]?\s*([ABCD])", text, flags=re.I
    )
    if len(answer_matches) >= len(questions) and questions:
        for i, q in enumerate(questions):
            q["correct"] = ord(answer_matches[i].upper()) - ord("A")
            q.pop("_needs_answer_key", None)
        return questions

    return []
