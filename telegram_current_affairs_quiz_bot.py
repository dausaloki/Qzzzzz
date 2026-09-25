# Telegram Current Affairs Quiz Bot
# PDF: 22 September 2026 Current Affairs (10 questions)
#
# Install:
#   pip install python-telegram-bot==22.5
#
# Run:
#   1) Create a bot with @BotFather and copy the token.
#   2) Put the token below in BOT_TOKEN.
#   3) Run: python quiz_bot.py
#   4) Open your bot and press "📚 आज की 10 प्रश्न Quiz"
#
# The bot sends all 10 questions as Telegram native QUIZ polls.

import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

QUIZ = [
    {
        "question": "विश्व गैंडा दिवस हर साल कब मनाया जाता है?",
        "options": ["21 सितंबर", "22 सितंबर", "23 सितंबर", "24 सितंबर"],
        "correct": 1,
        "explanation": "विश्व गैंडा दिवस हर साल 22 सितंबर को मनाया जाता है।"
    },
    {
        "question": "विश्व नदी दिवस हर साल कब मनाया जाता है?",
        "options": ["20 सितंबर", "21 सितंबर", "22 सितंबर", "23 सितंबर"],
        "correct": 2,
        "explanation": "इस PDF में दिए गए प्रश्न के अनुसार सही उत्तर 22 सितंबर है।"
    },
    {
        "question": "हाल ही में 20वें एशियाई खेलों का आयोजन किस देश में किया जाएगा?",
        "options": ["भारत", "जापान", "श्रीलंका", "इनमें से कोई नहीं"],
        "correct": 1,
        "explanation": "PDF के अनुसार 20वें एशियाई खेल 2026 का आयोजन जापान के आइची-नागोया क्षेत्र में किया जा रहा है।"
    },
    {
        "question": "हाल ही में कहाँ बनी फीचर फिल्म नुमाहांगमा को दूसरे नेपाल इंटरनेशनल फिल्म फेस्टिवल 2026 के लिए चुना गया है?",
        "options": ["सिक्किम", "असम", "मणिपुर", "मिजोरम"],
        "correct": 0,
        "explanation": "PDF के अनुसार फीचर फिल्म नुमाहांगमा सिक्किम में बनी है।"
    },
    {
        "question": "VB-G राम जी युवा डिजिटल अभियान किस मंत्रालय ने शुरू किया है?",
        "options": ["पंचायती राज मंत्रालय", "युवा मामले और खेल मंत्रालय", "ग्रामीण विकास मंत्रालय", "कौशल विकास मंत्रालय"],
        "correct": 2,
        "explanation": "PDF के अनुसार यह अभियान ग्रामीण विकास मंत्रालय ने MY भारत पोर्टल के सहयोग से शुरू किया है।"
    },
    {
        "question": "हाल ही में किस राज्य सरकार ने OSWAS 3.0 सेवा लॉन्च की है?",
        "options": ["ओडिशा", "गुजरात", "पंजाब", "बिहार"],
        "correct": 0,
        "explanation": "PDF के अनुसार OSWAS 3.0 परियोजना ओडिशा सरकार से संबंधित है।"
    },
    {
        "question": "हाल ही में किस राज्य में एप्टिनिडिया हाइकाई नामक कीवी की एक नई जंगली प्रजाति की खोज की गई है?",
        "options": ["असम", "मिजोरम", "त्रिपुरा", "अरुणाचल प्रदेश"],
        "correct": 3,
        "explanation": "PDF के अनुसार नई जंगली कीवी प्रजाति एप्टिनिडिया हाइकाई की खोज अरुणाचल प्रदेश में की गई है।"
    },
    {
        "question": "वर्ष 2026 में आयोजित पहले खेलो इंडिया ट्राइबल गेम्स की मेजबानी किस राज्य ने की है?",
        "options": ["ओडिशा", "झारखंड", "छत्तीसगढ़", "बिहार"],
        "correct": 2,
        "explanation": "PDF के अनुसार पहले खेलो इंडिया ट्राइबल गेम्स की मेजबानी छत्तीसगढ़ ने की है।"
    },
    {
        "question": "ट्रेड रिसीवेबल्स डिस्काउंटिंग सिस्टम प्लेटफॉर्म को किस प्राधिकरण द्वारा विनियमित किया जाता है?",
        "options": ["भारतीय रिजर्व बैंक", "भारतीय प्रतिभूति और विनिमय बोर्ड", "वित्त मंत्रालय", "राष्ट्रीय कृषि और ग्रामीण विकास बैंक"],
        "correct": 0,
        "explanation": "PDF के अनुसार TReDS प्लेटफॉर्म को भारतीय रिजर्व बैंक द्वारा विनियमित किया जाता है।"
    },
    {
        "question": "माटुआ समुदाय मुख्य रूप से किस राज्य में पाया जाता है?",
        "options": ["गुजरात", "मध्यप्रदेश", "राजस्थान", "पश्चिम बंगाल"],
        "correct": 3,
        "explanation": "PDF के अनुसार माटुआ समुदाय मुख्य रूप से पश्चिम बंगाल में पाया जाता है।"
    },
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📚 आज की 10 प्रश्न Quiz", callback_data="start_quiz")]
    ]
    await update.message.reply_text(
        "📝 Daily Current Affairs Quiz\n\n"
        "22 सितंबर करेंट अफेयर्स\n"
        "कुल प्रश्न: 10\n"
        "हर प्रश्न में 4 विकल्प हैं।\n\n"
        "नीचे बटन दबाकर Quiz शुरू करें 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Prevent accidental double-clicks from sending two sets.
    if context.user_data.get("quiz_sending"):
        await query.message.reply_text("⏳ Quiz पहले से भेजी जा रही है...")
        return

    context.user_data["quiz_sending"] = True

    try:
        await query.message.reply_text(
            "🚀 Quiz शुरू!\n\n"
            "10 में से प्रत्येक प्रश्न का सही विकल्प चुनें।"
        )

        for number, item in enumerate(QUIZ, start=1):
            await context.bot.send_poll(
                chat_id=query.message.chat_id,
                question=f"{number}. {item['question']}",
                options=item["options"],
                type="quiz",
                correct_option_id=item["correct"],
                is_anonymous=False,
                explanation=item["explanation"],
                explanation_parse_mode=None,
            )
            # Small delay keeps the questions in clean order.
            await asyncio.sleep(0.7)

        await query.message.reply_text(
            "✅ सभी 10 प्रश्न पूरे हो गए।\n"
            "अब अपने Quiz answers/review देख सकते हैं।"
        )

    finally:
        context.user_data["quiz_sending"] = False


def main():
    if not BOT_TOKEN:
        raise SystemExit("Railway Variables में BOT_TOKEN सेट करें।")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start_quiz, pattern="^start_quiz$"))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
