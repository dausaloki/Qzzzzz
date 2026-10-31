"""Generate original-content PDF samples, without Telegram, credentials or a DB.

Run from the project root: python scripts/generate_pdf_samples.py /path/to/output
Only synthetic examples are written. Never loads production quiz/user data.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz  # noqa: E402
import review_pdf as pdf  # noqa: E402


def sample_report(count: int = 8) -> pdf.Report:
    questions = []
    for i in range(1, count + 1):
        long = i % 3 == 0
        text = (f"मौलिक अभ्यास {i}: एक आयत की लंबाई 8 cm और चौड़ाई 3 cm है। उसका क्षेत्रफल चुनिए।"
                if i % 2 else f"मौलिक अभ्यास {i}: 'कमल' शब्द का आरंभिक अक्षर कौन-सा है?")
        if long:
            text += "\n" + "चित्र बनाए बिना दी गई लंबाई और चौड़ाई के गुणनफल पर विचार कीजिए। " * 5
        options = ["24 cm²", "11 cm²", "5 cm²", "48 cm²"] if i % 2 else ["क", "म", "ल", "अ"]
        explanation = ("क्षेत्रफल = लंबाई × चौड़ाई = 8 × 3 = 24 cm²। "
                       "\nगणित संकेत जाँच: x² + y² = r²; ½ + ¼ = ¾; √16 = 4; a ≤ b; θ = 30°।"
                       if i % 2 else "बाएँ से पढ़ने पर 'कमल' का पहला अक्षर क है। संयुक्ताक्षर अभ्यास: क्ष त्र ज्ञ श्र।")
        if long:
            explanation += "\n" + "1. जानकारी पढ़ें। 2. गणना करें। 3. विकल्प मिलाएँ। " * 18
        status = ("correct", "wrong", "unanswered")[(i - 1) % 3]
        questions.append(pdf.ReportQuestion(
            i, text, options, 0 if status == "correct" else (1 if status == "wrong" else None),
            [0], status, explanation, "अक्षर और गणित", skipped=status == "unanswered"))
    correct = sum(q.status == "correct" for q in questions)
    wrong = sum(q.status == "wrong" for q in questions)
    return pdf.Report("हिन्दी मोबाइल पठनीयता — मौलिक नमूना", "Sample participant (synthetic)", "private",
                      "2026-10-30 00:00 UTC", "2026-10-30 05:30 IST", count, correct + wrong,
                      correct, wrong, count - correct - wrong, count - correct - wrong,
                      correct, count, round(100 * correct / count, 2) if count else 0,
                      questions=questions)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/quiz_pdf_samples")
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for count in (1, 8, 50):
        raw, pages = pdf.render_report(sample_report(count))
        name = f"hindi_review_{count:02d}_questions.pdf"
        (out / name).write_bytes(raw)
        doc = fitz.open(stream=raw, filetype="pdf")
        # A true 390px-wide viewport render, not a cropped/zoomed representation.
        chosen = next((p.number for p in doc if "प्रश्न 1" in p.get_text()), 0)
        page = doc[chosen]
        page.get_pixmap(matrix=fitz.Matrix(390 / page.rect.width, 390 / page.rect.width),
                        alpha=False).save(str(out / f"mobile_390px_{count:02d}.png"))
        page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).save(str(out / f"desktop_{count:02d}.png"))
        results.append({"file": name, "questions": count, "pages": pages,
                        "mobile_width_px": 390, "preview_page": chosen + 1})
        doc.close()
    (out / "samples.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
