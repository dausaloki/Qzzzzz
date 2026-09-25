# Telegram Quiz Bot

Features:
- Group/private chat support
- One question at a time
- 30 second timeout
- User-specific sessions
- Stop quiz and show result
- Up to 100 questions
- Admin PDF upload workflow
- Railway worker deployment

Railway Variables:
BOT_TOKEN=your_botfather_token
ADMIN_IDS=comma_separated_telegram_user_ids

Important:
The included PDF parser intentionally does not guess answers. A PDF must contain a recognizable answer key for automatic import. Scanned PDFs/OCR require an OCR layer before automatic import.
