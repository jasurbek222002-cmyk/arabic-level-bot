"""
Jasur Arabic Bot — Arabic Language Assessment Bot for Telegram
=============================================================
Evaluates Writing and Speaking skills, assigns CEFR levels,
and gives detailed feedback in Arabic using Google Gemini AI.
"""

import os
import logging
import asyncio
import tempfile
import subprocess
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import google.generativeai as genai
import httpx

# ──────────────────────────────────────────────
# SETUP LOGGING  (you'll see these in Railway logs)
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# LOAD ENVIRONMENT VARIABLES
# ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# ──────────────────────────────────────────────
# USER SESSION STORAGE  (in memory — resets on restart)
# For production, use a database like Redis or PostgreSQL
# ──────────────────────────────────────────────
user_sessions: dict[int, dict] = {}

MODES = {
    "writing": "✍️ تقييم الكتابة",
    "speaking": "🎤 تقييم المحادثة",
}


# ══════════════════════════════════════════════
# HELPER: TRANSCRIBE VOICE WITH GEMINI
# ══════════════════════════════════════════════
async def transcribe_voice_with_gemini(ogg_bytes: bytes) -> str:
    """
    Send the OGG audio bytes to Gemini and ask it to transcribe
    the Arabic speech to text.
    """
    # Upload the audio file to Gemini Files API
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(ogg_bytes)
        tmp_path = tmp.name

    try:
        audio_file = genai.upload_file(tmp_path, mime_type="audio/ogg")
        response = model.generate_content(
            [
                audio_file,
                (
                    "Please transcribe this Arabic audio message accurately. "
                    "Return ONLY the transcribed Arabic text, nothing else. "
                    "If the audio is not in Arabic, say: [ليس عربياً]"
                ),
            ]
        )
        return response.text.strip()
    finally:
        os.unlink(tmp_path)


# ══════════════════════════════════════════════
# HELPER: ASSESS WITH GEMINI
# ══════════════════════════════════════════════
async def assess_arabic(text: str, mode: str) -> str:
    """
    Send the student's Arabic text to Gemini for CEFR assessment.
    Returns detailed feedback in Arabic.
    """
    mode_label = "الكتابة" if mode == "writing" else "الكلام (نص محوّل من صوت)"

    prompt = f"""
أنت خبير في اللغة العربية ومقيّم معتمد لمستويات CEFR.
مهمتك: تقييم النص العربي التالي الذي أرسله طالب يتعلم اللغة العربية.

**نوع التقييم:** {mode_label}

**النص المُقيَّم:**
"{text}"

قدّم تقييمًا شاملاً ومفصّلاً باللغة العربية يشمل:

1. **المستوى المُحدَّد** — حدّد المستوى من: A1 / A2 / B1 / B2 / C1 / C2
   واشرح معنى هذا المستوى باختصار.

2. **نقاط القوة** ✅ — اذكر ما أجاده الطالب تحديدًا (مفردات، قواعد، أسلوب...).

3. **نقاط التحسين** ⚠️ — اذكر الأخطاء أو الجوانب التي تحتاج تطوير مع أمثلة.

4. **التصحيح** 📝 — إذا كان هناك أخطاء نحوية أو إملائية، اعرض الجملة المصحّحة.

5. **تمرين للتحسين** 💡 — اقترح تمرينًا أو نشاطًا محددًا يساعد الطالب على تحسين مستواه.

6. **كلمة تشجيعية** 🌟 — اختم بجملة تحفيزية مناسبة.

استخدم نبرة ودية ومشجعة، واجعل الردّ واضحًا ومنظّمًا.
"""

    response = model.generate_content(prompt)
    return response.text.strip()


# ══════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message with mode selection buttons."""
    user = update.effective_user
    user_id = user.id

    # Reset session
    user_sessions[user_id] = {"mode": None}

    welcome = (
        f"السَّلَامُ عَلَيْكُم، {user.first_name}! 👋\n\n"
        "أَهْلًا بِكَ فِي بُوتِ **جَاسُور عَرَبِيك** لِتَقْيِيمِ مَهَارَاتِكَ فِي اللُّغَةِ الْعَرَبِيَّةِ.\n\n"
        "🎯 سَأُحَدِّدُ مُسْتَوَاكَ وَفقًا لِإطَارِ CEFR الْأُورُبِّيِّ:\n"
        "A1 ◀ A2 ◀ B1 ◀ B2 ◀ C1 ◀ C2\n\n"
        "اخْتَرْ نَوْعَ التَّقْيِيمِ:"
    )

    keyboard = [
        [
            InlineKeyboardButton("✍️ تقييم الكتابة", callback_data="mode_writing"),
            InlineKeyboardButton("🎤 تقييم الكلام", callback_data="mode_speaking"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome, reply_markup=reply_markup, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    help_text = (
        "📖 **كَيْفَ تَسْتَخْدِمُ الْبُوتَ؟**\n\n"
        "1️⃣ اضغط /start لِبَدْءِ جَلْسَةِ تَقْيِيمٍ جَدِيدَةٍ\n"
        "2️⃣ اخْتَرْ نَوْعَ التَّقْيِيمِ (كِتَابَة أَوْ كَلَام)\n"
        "3️⃣ أَرْسِلْ رَسَالَتَكَ بِالْعَرَبِيَّةِ\n"
        "4️⃣ انْتَظِرْ التَّقْيِيمَ الشَّامِلَ!\n\n"
        "**✍️ تَقْيِيمُ الْكِتَابَةِ:**\n"
        "أَرْسِلْ أَيَّ نَصٍّ عَرَبِيٍّ — جُمْلَةً، فَقْرَةً، أَوْ قِصَّةً قَصِيرَةً.\n\n"
        "**🎤 تَقْيِيمُ الْكَلَامِ:**\n"
        "أَرْسِلْ رِسَالَةً صَوْتِيَّةً وَسَأُحَوِّلُهَا إِلَى نَصٍّ وَأُقَيِّمُهَا.\n\n"
        "الأَوَامِرُ:\n"
        "/start — بَدْءُ تَقْيِيمٍ جَدِيدٍ\n"
        "/help — عَرْضُ هَذِهِ الْمُسَاعَدَةِ"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# ══════════════════════════════════════════════
# CALLBACK HANDLER  (button presses)
# ══════════════════════════════════════════════
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Remove the loading spinner

    user_id = query.from_user.id
    data = query.data

    if data == "mode_writing":
        user_sessions[user_id] = {"mode": "writing"}
        await query.edit_message_text(
            "✍️ **وَضْعُ تَقْيِيمِ الْكِتَابَةِ**\n\n"
            "أَرْسِلْ لِي أَيَّ نَصٍّ عَرَبِيٍّ تُرِيدُ تَقْيِيمَهُ.\n\n"
            "💡 نَصِيحَةٌ: كُلَّمَا كَانَ النَّصُّ أَطْوَلَ، كَانَ التَّقْيِيمُ أَدَقَّ!",
            parse_mode="Markdown",
        )

    elif data == "mode_speaking":
        user_sessions[user_id] = {"mode": "speaking"}
        await query.edit_message_text(
            "🎤 **وَضْعُ تَقْيِيمِ الْكَلَامِ**\n\n"
            "أَرْسِلْ لِي رِسَالَةً صَوْتِيَّةً بِالْعَرَبِيَّةِ.\n\n"
            "💡 نَصِيحَةٌ: تَكَلَّمْ بِوُضُوحٍ وَبِجُمَلٍ كَامِلَةٍ لِلْحُصُولِ عَلَى أَفْضَلِ تَقْيِيمٍ!",
            parse_mode="Markdown",
        )

    elif data == "restart":
        user_sessions[user_id] = {"mode": None}
        keyboard = [
            [
                InlineKeyboardButton("✍️ تقييم الكتابة", callback_data="mode_writing"),
                InlineKeyboardButton("🎤 تقييم الكلام", callback_data="mode_speaking"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔄 جَلْسَةٌ جَدِيدَةٌ! اخْتَرْ نَوْعَ التَّقْيِيمِ:",
            reply_markup=reply_markup,
        )


# ══════════════════════════════════════════════
# TEXT MESSAGE HANDLER
# ══════════════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages — used for writing assessment."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    mode = session.get("mode")

    if not mode:
        await update.message.reply_text(
            "الرَّجَاءُ اضغط /start لِبَدْءِ التَّقْيِيمِ وَاخْتِيَارِ النَّوْعِ أَوَّلًا."
        )
        return

    if mode == "speaking":
        await update.message.reply_text(
            "🎤 أَنْتَ فِي وَضْعِ تَقْيِيمِ الْكَلَامِ.\n"
            "الرَّجَاءُ أَرْسِلْ رِسَالَةً صَوْتِيَّةً، لَيْسَ نَصًّا.\n\n"
            "أَوِ اضغط /start لِتَغْيِيرِ الْوَضْعِ."
        )
        return

    text = update.message.text.strip()
    if len(text) < 5:
        await update.message.reply_text(
            "الرَّجَاءُ أَرْسِلْ نَصًّا أَطْوَلَ (٥ أَحْرُفٍ عَلَى الْأَقَلِّ)."
        )
        return

    # Show typing indicator
    await update.message.chat.send_action("typing")
    await update.message.reply_text("⏳ جَارٍ تَحْلِيلُ نَصِّكَ... انْتَظِرْ لَحْظَةً.")

    try:
        feedback = await assess_arabic(text, "writing")
        response_text = (
            "📊 **نَتِيجَةُ تَقْيِيمِ الْكِتَابَةِ**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{feedback}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = [[InlineKeyboardButton("🔄 تَقْيِيمٌ جَدِيدٌ", callback_data="restart")]]
        await update.message.reply_text(
            response_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Assessment error: {e}")
        await update.message.reply_text(
            "⚠️ حَدَثَ خَطَأٌ أَثْنَاءَ التَّقْيِيمِ. الرَّجَاءُ حَاوِلْ مَرَّةً أُخْرَى.\n\n"
            f"تَفَاصِيلُ الْخَطَأِ: {str(e)[:100]}"
        )


# ══════════════════════════════════════════════
# VOICE MESSAGE HANDLER
# ══════════════════════════════════════════════
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages — transcribe then assess."""
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    mode = session.get("mode")

    if not mode:
        await update.message.reply_text(
            "الرَّجَاءُ اضغط /start لِبَدْءِ التَّقْيِيمِ وَاخْتِيَارِ النَّوْعِ أَوَّلًا."
        )
        return

    if mode == "writing":
        await update.message.reply_text(
            "✍️ أَنْتَ فِي وَضْعِ تَقْيِيمِ الْكِتَابَةِ.\n"
            "الرَّجَاءُ أَرْسِلْ نَصًّا مَكْتُوبًا، لَيْسَ رِسَالَةً صَوْتِيَّةً.\n\n"
            "أَوِ اضغط /start لِتَغْيِيرِ الْوَضْعِ."
        )
        return

    await update.message.reply_text("🎧 جَارٍ تَحْلِيلُ رِسَالَتِكَ الصَّوْتِيَّةِ... انْتَظِرْ لَحْظَةً.")
    await update.message.chat.send_action("typing")

    try:
        # Download the voice file from Telegram
        voice = update.message.voice
        voice_file = await context.bot.get_file(voice.file_id)

        async with httpx.AsyncClient() as client:
            response = await client.get(voice_file.file_path)
            ogg_bytes = response.content

        # Step 1: Transcribe
        await update.message.reply_text("🔤 جَارٍ تَحْوِيلُ الصَّوْتِ إِلَى نَصٍّ...")
        transcribed_text = await transcribe_voice_with_gemini(ogg_bytes)

        if "[ليس عربياً]" in transcribed_text:
            await update.message.reply_text(
                "⚠️ لَمْ أَتَمَكَّنْ مِنَ التَّعَرُّفِ عَلَى كَلَامٍ عَرَبِيٍّ فِي هَذِهِ الرِّسَالَةِ.\n"
                "الرَّجَاءُ أَرْسِلْ رِسَالَةً صَوْتِيَّةً بِالْعَرَبِيَّةِ."
            )
            return

        await update.message.reply_text(
            f"📝 **النَّصُّ الْمُحَوَّلُ مِنَ الصَّوْتِ:**\n\n_{transcribed_text}_",
            parse_mode="Markdown",
        )

        # Step 2: Assess the transcribed text
        await update.message.reply_text("📊 جَارٍ تَقْيِيمُ كَلَامِكَ...")
        feedback = await assess_arabic(transcribed_text, "speaking")

        response_text = (
            "🎤 **نَتِيجَةُ تَقْيِيمِ الْكَلَامِ**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{feedback}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = [[InlineKeyboardButton("🔄 تَقْيِيمٌ جَدِيدٌ", callback_data="restart")]]
        await update.message.reply_text(
            response_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text(
            "⚠️ حَدَثَ خَطَأٌ أَثْنَاءَ مَعَالَجَةِ الرِّسَالَةِ الصَّوْتِيَّةِ.\n"
            "الرَّجَاءُ حَاوِلْ مَرَّةً أُخْرَى أَوِ اضغط /start.\n\n"
            f"تَفَاصِيلُ الْخَطَأِ: {str(e)[:100]}"
        )


# ══════════════════════════════════════════════
# MAIN — START THE BOT
# ══════════════════════════════════════════════
def main() -> None:
    """Entry point — build and run the bot."""
    logger.info("🚀 Starting Jasur Arabic Bot...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("✅ Bot is running. Waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
