# Telegram Quiz Bot — Railway image with Hindi + English OCR support.
#
# PyMuPDF ships its own Tesseract engine; it only needs the language model
# files.  We pin the EXACT files the test-suite is verified with (tessdata
# 4.1.0 "standard" models, checked by sha256).  Debian's apt OCR language
# packages are NOT used: they contain the smaller "fast" models, which
# measurably misread Hindi (क्या → कया) and dropped answer-key letters in tests.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

COPY scripts/fetch_tessdata.py /tmp/fetch_tessdata.py
RUN python /tmp/fetch_tessdata.py && rm /tmp/fetch_tessdata.py

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# Build fails here if OCR would not work at runtime.
RUN python -c "import pdf_extract as X; ok, info = X.ocr_status(); print('OCR:', ok, info); assert ok, info"

# Exec form: python is PID 1 and receives Railway's SIGTERM directly
# (python-telegram-bot then stops polling cleanly).  railway.json's
# startCommand is identical and Railway also runs it in exec form.
CMD ["python", "bot.py"]
