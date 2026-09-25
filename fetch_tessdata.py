"""Download the pinned Tesseract models used by the Docker image (and tests).

Usage: TESSDATA_PREFIX=/path python scripts/fetch_tessdata.py
tessdata 4.1.0 "standard" models, verified by sha256 — the exact files the
test-suite passes with.
"""
import hashlib, os, urllib.request
models = {
    "eng": "daa0c97d651c19fba3b25e81317cd697e9908c8208090c94c3905381c23fc047",
    "hin": "cc76d09fa4fed1c7a4674046e25e63760d0c9bfdce390a52113462c34a556ee6",
}
dest = os.environ["TESSDATA_PREFIX"]
os.makedirs(dest, exist_ok=True)
for lang, sha in models.items():
    url = f"https://github.com/tesseract-ocr/tessdata/raw/4.1.0/{lang}.traineddata"
    data = urllib.request.urlopen(url, timeout=120).read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        raise SystemExit(f"{lang}.traineddata checksum mismatch: {got}")
    with open(os.path.join(dest, f"{lang}.traineddata"), "wb") as fh:
        fh.write(data)
    print(f"installed {lang}.traineddata ({len(data)} bytes, sha256 ok)")
