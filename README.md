# QuizBot Pro — Railway Ready

यह bot Telegram के native Quiz Poll पर आधारित है और @QuizBot के मुख्य workflow को करीब से replicate करता है।

## Features

- `/start` main menu
- New Quiz wizard
- Quiz title + description
- Pre-question text
- Manual question creation
- 4 answer options
- Correct answer selection
- Explanation
- `/done`
- Timer: 10s, 15s, 30s, 60s, 90s, 2m, 3m, 5m, No timer
- Shuffle questions
- Shuffle options
- My Quizzes
- Edit title/description
- Add/delete questions
- Delete quiz
- Quiz Stats
- Shareable private quiz link
- Native Telegram quiz polls
- PDF/Text import
- Maximum 100 questions
- SQLite persistence

## Railway

Environment variable:

`BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN`

Deploy command:

`python bot.py`

`Procfile` और `railway.json` पहले से मौजूद हैं।

## Important Telegram limits

Telegram native poll question में अधिकतम 300 characters और प्रत्येक poll option में अधिकतम 100 characters हैं। इसलिए source का पूरा लंबा question/options पहले normal Telegram message में दिखाया जाता है और फिर native poll दिया जाता है। अगर option 100 characters से लंबा है तो native poll में A/B/C/D labels आते हैं, जबकि पूरा option ऊपर के source message में सुरक्षित रहता है।

## PDF

Selectable-text PDF supported है। Scanned/image-only PDF के लिए OCR की जरूरत होगी।

## Group behavior

Independent user-by-user sequential sessions private chat में चलाए जाते हैं। Group में bot share link देता है, जिससे हर participant अपना private session शुरू कर सकता है। इससे एक participant का Question 2 दूसरे participant के Question 1 से conflict नहीं करता।
