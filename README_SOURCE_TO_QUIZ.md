# Source-to-Quiz update

The question bank is NOT fixed.

Admin sends:
- PDF
- TXT
- or plain text MCQs

The bot parses the source and replaces data/questions.json.

Safety rule:
The parser never guesses a correct answer. A question is imported only when
an explicit answer key is found.

Supported answer examples:
- Answer: B
- Ans: C
- उत्तर: D
- 1-A, 2-C, 3-B
- 1. A  2. C  3. B

The PDF must contain selectable/extractable text. Scanned image PDFs require
an OCR layer before they can be parsed reliably.
