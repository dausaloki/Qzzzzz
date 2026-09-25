RAILWAY DEPLOYMENT - TELEGRAM QUIZ BOT

1. इस ZIP को extract करके GitHub repository में files upload करें:
   - telegram_current_affairs_quiz_bot.py
   - requirements.txt
   - Procfile

2. Railway में New Project -> Deploy from GitHub Repo चुनें।

3. Railway -> Variables में यह variable बनाएं:
   Name: BOT_TOKEN
   Value: @BotFather से मिला Telegram Bot Token

4. Deploy करें।

5. Logs में "Bot started..." दिखे तो bot चालू है।

6. Telegram में bot खोलें और /start भेजें।
   फिर "📚 आज की 10 प्रश्न Quiz" दबाएं।

महत्वपूर्ण:
- Bot Token को code/GitHub में न डालें।
- Railway में BOT_TOKEN variable में ही रखें।
