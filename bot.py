"""
╔══════════════════════════════════════════════════════════════════════╗
║   بُوتُ مُحَاكَاةِ اخْتِبَارِ التَّنَالِ الْعَرَبِيِّ              ║
║   At-Tanal al-Arabi Exam Simulator — Jasur Arabic Channel           ║
║   Engine: Gemini 2.5 Flash  |  5 Modules  |  150 Points Max        ║
╚══════════════════════════════════════════════════════════════════════╝

State machine architecture:
  IDLE → MODULE_MENU → [GRAMMAR | READING | LISTENING | SPEAKING | WRITING]
  Each module has its own sub-states managed via user_sessions dict.
"""

import os
import json
import logging
import asyncio
import tempfile
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ENV & GEMINI CLIENT
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-2.5-flash"

# ─────────────────────────────────────────────
# SESSION STORE  { user_id: { ... } }
# ─────────────────────────────────────────────
sessions: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════════════════
# TOP-LEVEL STATES
# ══════════════════════════════════════════════════════════════════════
ST_IDLE          = "IDLE"
ST_MENU          = "MENU"

# Grammar
ST_GR_ACTIVE     = "GR_ACTIVE"      # answering a question
ST_GR_DONE       = "GR_DONE"

# Reading
ST_RD_PART       = "RD_PART"        # reading a passage
ST_RD_ACTIVE     = "RD_ACTIVE"
ST_RD_DONE       = "RD_DONE"

# Listening
ST_LI_ACTIVE     = "LI_ACTIVE"
ST_LI_DONE       = "LI_DONE"

# Speaking
ST_SP_PROMPT     = "SP_PROMPT"      # waiting for voice note
ST_SP_DONE       = "SP_DONE"

# Writing
ST_WR_PROMPT     = "WR_PROMPT"      # waiting for essay text
ST_WR_DONE       = "WR_DONE"

# ══════════════════════════════════════════════════════════════════════
# UTILITY: call Gemini safely
# ══════════════════════════════════════════════════════════════════════
async def gemini_text(prompt: str, system: str = "") -> str:
    """Non-streaming Gemini call, returns text."""
    cfg = types.GenerateContentConfig(
        system_instruction=system if system else None,
        temperature=0.4,
        max_output_tokens=4096,
    )
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=GEMINI_MODEL,
        contents=prompt,
        config=cfg,
    )
    return response.text.strip()


async def gemini_audio(audio_bytes: bytes, mime: str, prompt: str) -> str:
    """Send audio bytes inline to Gemini and get text back."""
    part = types.Part.from_bytes(data=audio_bytes, mime_type=mime)
    cfg = types.GenerateContentConfig(temperature=0.3, max_output_tokens=3000)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=GEMINI_MODEL,
        contents=[part, prompt],
        config=cfg,
    )
    return response.text.strip()


# ══════════════════════════════════════════════════════════════════════
# KEYBOARD HELPERS
# ══════════════════════════════════════════════════════════════════════
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 الْقَوَاعِدُ وَالْمُفْرَدَاتُ", callback_data="mod_grammar")],
        [InlineKeyboardButton("📖 فَهْمُ الْمَقْرُوءِ", callback_data="mod_reading")],
        [InlineKeyboardButton("🎧 فَهْمُ الْمَسْمُوعِ", callback_data="mod_listening")],
        [InlineKeyboardButton("🎤 مَهَارَةُ التَّحَدُّثِ", callback_data="mod_speaking")],
        [InlineKeyboardButton("✍️ مَهَارَةُ الْكِتَابَةِ", callback_data="mod_writing")],
    ])


def abcd_kb(q_idx: int) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("أ", callback_data=f"ans_{q_idx}_A"),
        InlineKeyboardButton("ب", callback_data=f"ans_{q_idx}_B"),
    ]
    row2 = [
        InlineKeyboardButton("ج", callback_data=f"ans_{q_idx}_C"),
        InlineKeyboardButton("د", callback_data=f"ans_{q_idx}_D"),
    ]
    return InlineKeyboardMarkup([row1, row2])


def next_kb(label: str, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)]])


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 الْقَائِمَةُ الرَّئِيسِيَّةُ", callback_data="back_menu")]
    ])


# ══════════════════════════════════════════════════════════════════════
# MODULE 1 — GRAMMAR & VOCABULARY  (القواعد والمفردات)
# ══════════════════════════════════════════════════════════════════════
GRAMMAR_SYSTEM = """
أَنْتَ مُحَاكِي اخْتِبَارِ التَّنَالِ الْعَرَبِيِّ الرَّسْمِيِّ.
يَجِبُ أَنْ تُنْشِئَ أَسْئِلَةً اخْتِبَارِيَّةً دَقِيقَةً وَصَارِمَةً تَعْكِسُ الْمَنْهَجَ الرَّسْمِيَّ.
لَا مَجَالَ لِلْمُجَامَلَةِ أَوِ الثَّنَاءِ — فَقَطِ الدَّقَّةُ الأَكَادِيمِيَّةُ.
"""

GRAMMAR_PROMPT_TEMPLATE = """
أَنْشِئْ سُؤَالًا اخْتِبَارِيًّا رَقْمُهُ {num} (مِنْ 30) مِنِ اخْتِبَارِ التَّنَالِ الْعَرَبِيِّ.

الْمُسْتَوَى: {tier}
الْمَوْضُوعُ الْمُحَدَّدُ: {topic}

الشَّرْطُ: أَرْبَعَةُ خِيَارَاتٍ (أ، ب، ج، د) — خِيَارٌ وَاحِدٌ صَحِيحٌ فَقَطْ.

أَرْجِعِ النَّتِيجَةَ بِصِيغَةِ JSON فَقَطْ (بِلَا مَسَافَاتٍ إِضَافِيَّةٍ أَوْ أَكْوَادٍ):
{{
  "question": "نَصُّ السُّؤَالِ بِالْعَرَبِيَّةِ الْمُشَكَّلَةِ",
  "A": "الخيار أ",
  "B": "الخيار ب",
  "C": "الخيار ج",
  "D": "الخيار د",
  "correct": "A",
  "explanation_uz": "To'g'ri javob va grammatik tushuntirish o'zbekcha..."
}}
"""

GRAMMAR_TIERS = [
    # Tier 1 (Q1-10): Easy — foundational Nahv, Sarf, Imla
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "مَوَازِينُ الْأَفْعَالِ الأَسَاسِيَّةُ وَالتَّشْكِيلُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الْفِعْلُ الْمَاضِي وَالْمُضَارِعُ وَصِيَغُهُمَا"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الِاسْمُ الْمَعْرِفَةُ وَالنَّكِرَةُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الضَّمَائِرُ الْمُتَّصِلَةُ وَالْمُنْفَصِلَةُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الإِمْلَاءُ: الْأَلِفُ اللَّيِّنَةُ وَالتَّاءُ الْمَرْبُوطَةُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الْجُمْلَةُ الِاسْمِيَّةُ: الْمُبْتَدَأُ وَالْخَبَرُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الْجُمْلَةُ الْفِعْلِيَّةُ: الْفِعْلُ وَالْفَاعِلُ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الْمُفْرَدَاتُ الأَسَاسِيَّةُ وَالتَّعْرِيفُ بِالسِّيَاقِ"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "حُرُوفُ الْجَرِّ الشَّائِعَةُ وَاسْتِخْدَامُهَا"},
    {"tier": "السَّهْلُ (الْمُسْتَوَى الأَوَّلُ)", "topic": "الْمُذَكَّرُ وَالْمُؤَنَّثُ وَعَلَامَاتُهُمَا"},
    # Tier 2 (Q11-20): Intermediate
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الإِعْرَابُ: الرَّفْعُ وَالنَّصْبُ وَالْجَرُّ وَعَلَامَاتُهَا"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الْمَفَاعِيلُ: بِهِ وَلِأَجْلِهِ وَفِيهِ وَمَعَهُ"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الصِّفَةُ وَالْمَوْصُوفُ وَالتَّطَابُقُ"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "اسْمُ الْفَاعِلِ وَاسْمُ الْمَفْعُولِ وَأَوْزَانُهُمَا"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الْهَمْزَةُ: مَوَاضِعُ هَمْزَةِ الْوَصْلِ وَالْقَطْعِ"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الْجُمَلُ الشَّرْطِيَّةُ: إِنْ وَلَوْ وَإِذَا"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "الْمَصْدَرُ وَأَنْوَاعُهُ وَاسْتِخْدَامُهُ"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "التَّوَابِعُ: الْبَدَلُ وَعَطْفُ الْبَيَانِ"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "أَسَالِيبُ النَّفْيِ: لَا وَلَمْ وَلَنْ وَمَا"},
    {"tier": "الْمُتَوَسِّطُ (الْمُسْتَوَى الثَّانِي)", "topic": "أَسْمَاءُ الإِشَارَةِ وَالأَسْمَاءُ الْمَوْصُولَةُ"},
    # Tier 3 (Q21-30): Hard
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الأَفْعَالُ الْمُعْتَلَّةُ: الْمِثَالُ وَالأَجْوَفُ وَالنَّاقِصُ"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الأَفْعَالُ الْخَمْسَةُ وَرَفْعُهَا وَنَصْبُهَا وَجَزْمُهَا"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "نَوَاسِخُ الْجُمْلَةِ الِاسْمِيَّةِ: كَانَ وَأَخَوَاتُهَا وَإِنَّ وَأَخَوَاتُهَا"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الاسْتِثْنَاءُ: إِلَّا وَغَيْرُ وَسِوَى وَأَحْكَامُهَا"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الْجُمْلَةُ الْحَالِيَّةُ وَالتَّمْيِيزُ وَفُرُوقُهُمَا"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الأَسَالِيبُ الْبَلَاغِيَّةُ: التَّشْبِيهُ وَالِاسْتِعَارَةُ فِي السِّيَاقِ"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "دَقَائِقُ الْهَمْزَةِ الْمُتَوَسِّطَةِ وَالْمُتَطَرِّفَةِ"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الْفِعْلُ الْمَجْهُولُ وَنَائِبُ الْفَاعِلِ"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "الْأَفْعَالُ الْمُضَاعَفَةُ وَأَحْكَامُهَا الصَّرْفِيَّةُ"},
    {"tier": "الصَّعْبُ (الْمُسْتَوَى الثَّالِثُ)", "topic": "السِّيَاقُ الأُسْلُوبِيُّ وَاخْتِيَارُ الْمُفْرَدَةِ الدَّقِيقَةِ"},
]


async def grammar_generate_question(num: int) -> dict:
    """Generate one grammar MCQ via Gemini, return parsed dict."""
    tier_data = GRAMMAR_TIERS[num - 1]
    prompt = GRAMMAR_PROMPT_TEMPLATE.format(
        num=num,
        tier=tier_data["tier"],
        topic=tier_data["topic"],
    )
    raw = await gemini_text(prompt, system=GRAMMAR_SYSTEM)
    # Strip any markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: extract JSON block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"Cannot parse Gemini JSON: {raw[:200]}")


async def grammar_send_question(
    update_or_query, context: ContextTypes.DEFAULT_TYPE, session: dict
):
    """Generate and send the next grammar question."""
    q_num = session["gr_current"]  # 1-based
    await _send_typing(update_or_query, context)

    try:
        q_data = await grammar_generate_question(q_num)
    except Exception as e:
        logger.error(f"Grammar generation error: {e}")
        msg = "⚠️ Savol yaratishda xatolik. Qayta urinib ko'ring: /start"
        await _reply(update_or_query, msg)
        return

    session["gr_current_q"] = q_data
    tier_data = GRAMMAR_TIERS[q_num - 1]

    # Determine tier label
    if q_num <= 10:
        tier_badge = "🟢"
    elif q_num <= 20:
        tier_badge = "🟡"
    else:
        tier_badge = "🔴"

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{tier_badge} *السُّؤَالُ {q_num} مِنْ 30*\n"
        f"📌 {tier_data['topic']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{q_data['question']}\n\n"
        f"أ — {q_data['A']}\n"
        f"ب — {q_data['B']}\n"
        f"ج — {q_data['C']}\n"
        f"د — {q_data['D']}"
    )
    await _reply(update_or_query, text, kb=abcd_kb(q_num), parse_mode="Markdown")


async def grammar_handle_answer(
    query, context: ContextTypes.DEFAULT_TYPE, session: dict, chosen: str
):
    """Process user answer, show feedback, advance or finish."""
    await query.answer()
    q_data = session.get("gr_current_q", {})
    correct = q_data.get("correct", "A")
    q_num = session["gr_current"]
    is_correct = chosen == correct

    if is_correct:
        session["gr_score"] = session.get("gr_score", 0) + 1
        result_icon = "✅"
        result_text = "To'g'ri!"
    else:
        result_icon = "❌"
        result_text = f"Noto'g'ri. To'g'ri javob: *{correct}* — {q_data.get(correct, '')}"

    options_map = {"A": "أ", "B": "ب", "C": "ج", "D": "د"}
    feedback = (
        f"{result_icon} *{result_text}*\n\n"
        f"📝 *Izoh (tushuntirish):*\n{q_data.get('explanation_uz', '')}"
    )

    if q_num < 30:
        session["gr_current"] = q_num + 1
        kb = next_kb("التَّالِي ←", f"gr_next_{q_num + 1}")
    else:
        session["state"] = ST_GR_DONE
        score = session.get("gr_score", 0)
        pct = round((score / 30) * 100)
        feedback += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏁 *Yakuniy natija:* {score}/30 ({pct}%)\n"
        )
        if pct >= 80:
            feedback += "📊 Daraja: B2–C1 (Yuqori)"
        elif pct >= 60:
            feedback += "📊 Daraja: B1–B2 (O'rta)"
        elif pct >= 40:
            feedback += "📊 Daraja: A2–B1 (Quyi o'rta)"
        else:
            feedback += "📊 Daraja: A1–A2 (Boshlang'ich)"
        kb = back_to_menu_kb()
        await query.edit_message_text(feedback, parse_mode="Markdown", reply_markup=kb)
        return

    await query.edit_message_text(feedback, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════
# MODULE 2 — READING COMPREHENSION  (فهم المقروء)
# ══════════════════════════════════════════════════════════════════════
READING_SYSTEM = """
أَنْتَ مُعِدُّ اخْتِبَارِ التَّنَالِ الْعَرَبِيِّ.
أَنْشِئْ نُصُوصًا أَكَادِيمِيَّةً حَقِيقِيَّةً تُشَابِهُ مَوَادَّ الِاخْتِبَارِ الرَّسْمِيَّةِ.
الْأَسْئِلَةُ صَارِمَةٌ وَتَحْتَاجُ إِلَى قِرَاءَةٍ دَقِيقَةٍ.
"""

READING_PARTS = [
    {
        "part": 1,
        "label": "النَّصُّ الْقَصِيرُ",
        "desc": "نَصٌّ قَصِيرٌ (80-100 كَلِمَةٍ) — مَعْلُومَاتٌ مُبَاشِرَةٌ وَمَسْحٌ سَرِيعٌ",
        "length": "80-100 كلمة",
        "q_type": "مسح معلوماتي مباشر وخرائط دلالية أساسية",
    },
    {
        "part": 2,
        "label": "النَّصُّ الْمُتَوَسِّطُ",
        "desc": "نَصٌّ مُتَوَسِّطٌ (150-200 كَلِمَةٍ) — تَفَاصِيلُ وَمَعَانِي ضِمْنِيَّةٌ",
        "length": "150-200 كلمة",
        "q_type": "تفاصيل دقيقة ومعاني المفردات من السياق وأفكار الفقرات",
    },
    {
        "part": 3,
        "label": "النَّصُّ الطَّوِيلُ",
        "desc": "نَصٌّ طَوِيلٌ (250-300 كَلِمَةٍ) — اسْتِخْرَاجٌ أَكَادِيمِيٌّ شَامِلٌ",
        "length": "250-300 كلمة",
        "q_type": "استخراج أكاديمي شامل وحجج ومنطق النص",
    },
]

READING_PASSAGE_PROMPT = """
أَنْشِئْ نَصًّا قِرَائِيًّا لِاخْتِبَارِ التَّنَالِ الْعَرَبِيِّ.

النَّوْعُ: {label}
الطُّولُ الْمَطْلُوبُ: {length}
نَوْعُ الأَسْئِلَةِ: {q_type}

الْمَوْضُوعُ: اخْتَرْ مَوْضُوعًا مِنْ: التِّكْنُولُوجِيَا، الْبِيئَةُ، التَّعْلِيمُ، الصِّحَّةُ، الثَّقَافَةُ، الِاقْتِصَادُ.

أَرْجِعِ النَّتِيجَةَ بِصِيغَةِ JSON فَقَطْ:
{{
  "title": "عُنْوَانُ النَّصِّ",
  "passage": "النَّصُّ كَامِلًا بِالتَّشْكِيلِ...",
  "questions": [
    {{
      "q": "نَصُّ السُّؤَالِ",
      "A": "الخيار أ", "B": "الخيار ب", "C": "الخيار ج", "D": "الخيار د",
      "correct": "A",
      "explanation_uz": "Nima uchun bu javob to'g'ri..."
    }}
  ]
}}

يَجِبُ أَنْ تَكُونَ الأَسْئِلَةُ 6 بِالضَّبْطِ.
"""


async def reading_generate_part(part_info: dict) -> dict:
    prompt = READING_PASSAGE_PROMPT.format(**part_info)
    raw = await gemini_text(prompt, system=READING_SYSTEM)
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


async def reading_send_passage(update_or_query, context, session: dict):
    part_idx = session["rd_part"]  # 0-based index into READING_PARTS
    part_info = READING_PARTS[part_idx]
    await _send_typing(update_or_query, context)

    try:
        part_data = await reading_generate_part(part_info)
    except Exception as e:
        logger.error(f"Reading generation error: {e}")
        await _reply(update_or_query, "⚠️ Xatolik yuz berdi. /start bilan qaytadan boshlang.")
        return

    session["rd_part_data"] = part_data
    session["rd_q_idx"] = 0
    session["state"] = ST_RD_ACTIVE

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 *{part_info['label']}* — الْجُزْءُ {part_idx + 1} مِنْ 3\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📄 *{part_data.get('title', '')}*\n\n"
        f"{part_data.get('passage', '')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"اِقْرَأِ النَّصَّ جَيِّدًا، ثُمَّ أَجِبْ عَنِ الأَسْئِلَةِ السِّتَّةِ."
    )
    kb = next_kb("▶️ بَدْءُ الأَسْئِلَةِ", "rd_start_questions")
    await _reply(update_or_query, text, kb=kb, parse_mode="Markdown")


async def reading_send_question(update_or_query, context, session: dict):
    part_data = session["rd_part_data"]
    part_idx = session["rd_part"]
    q_idx = session["rd_q_idx"]
    questions = part_data.get("questions", [])

    if q_idx >= len(questions):
        # Move to next part or finish
        if part_idx < 2:
            session["rd_part"] = part_idx + 1
            session["rd_q_idx"] = 0
            kb = next_kb(f"▶️ الْجُزْءُ {part_idx + 2}", f"rd_next_part")
            await _reply(
                update_or_query,
                f"✅ *الْجُزْءُ {part_idx + 1} اكْتَمَلَ!*\nاضْغَطْ لِلِانْتِقَالِ إِلَى الْجُزْءِ التَّالِي.",
                kb=kb, parse_mode="Markdown"
            )
        else:
            session["state"] = ST_RD_DONE
            score = session.get("rd_score", 0)
            pct = round((score / 18) * 100)
            text = (
                f"🏁 *اكْتَمَلَ اخْتِبَارُ الْقِرَاءَةِ!*\n\n"
                f"النَّتِيجَةُ: {score}/18 ({pct}%)\n\n"
            )
            if pct >= 78:
                text += "📊 Daraja: B2–C1"
            elif pct >= 56:
                text += "📊 Daraja: B1–B2"
            else:
                text += "📊 Daraja: A2–B1"
            await _reply(update_or_query, text, kb=back_to_menu_kb(), parse_mode="Markdown")
        return

    q = questions[q_idx]
    passage = part_data.get("passage", "")
    # Show truncated passage reminder
    passage_short = passage[:400] + "..." if len(passage) > 400 else passage

    text = (
        f"📄 *مُقْتَطَفٌ مِنَ النَّصِّ:*\n_{passage_short}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ *السُّؤَالُ {q_idx + 1} مِنَ الْجُزْءِ {part_idx + 1}:*\n\n"
        f"{q['q']}\n\n"
        f"أ — {q['A']}\n"
        f"ب — {q['B']}\n"
        f"ج — {q['C']}\n"
        f"د — {q['D']}"
    )
    kb = abcd_kb(q_idx)
    await _reply(update_or_query, text, kb=kb, parse_mode="Markdown")


async def reading_handle_answer(query, context, session: dict, chosen: str):
    await query.answer()
    part_data = session["rd_part_data"]
    q_idx = session["rd_q_idx"]
    questions = part_data.get("questions", [])
    q = questions[q_idx]
    correct = q.get("correct", "A")
    is_correct = chosen == correct

    if is_correct:
        session["rd_score"] = session.get("rd_score", 0) + 1
        icon = "✅"
        label = "To'g'ri!"
    else:
        icon = "❌"
        label = f"Noto'g'ri. To'g'ri javob: *{correct}* — {q.get(correct, '')}"

    feedback = (
        f"{icon} *{label}*\n\n"
        f"📝 *Izoh:*\n{q.get('explanation_uz', '')}"
    )
    session["rd_q_idx"] = q_idx + 1
    kb = next_kb("التَّالِي ←", "rd_next_q")
    await query.edit_message_text(feedback, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════
# MODULE 3 — LISTENING COMPREHENSION  (فهم المسموع)
# ══════════════════════════════════════════════════════════════════════
LISTENING_SYSTEM = """
أَنْتَ مُعِدُّ اخْتِبَارِ الِاسْتِمَاعِ فِي التَّنَالِ الْعَرَبِيِّ.
أَنْشِئْ نُصُوصًا تُحَاكِي الْمَقَاطِعَ الصَّوْتِيَّةَ الرَّسْمِيَّةَ: حِوَارَاتٌ قَصِيرَةٌ وَطَوِيلَةٌ وَخِطَابَاتٌ أَكَادِيمِيَّةٌ.
"""

LISTENING_PARTS = [
    {
        "part": 1,
        "label": "الْحِوَارَاتُ الْقَصِيرَةُ",
        "format": """
أَنْشِئْ 6 حِوَارَاتٍ مُسْتَقِلَّةً قَصِيرَةً (3-4 أَسْطُرٍ لِكُلِّ وَاحِدٍ) بَيْنَ رَجُلٍ وَامْرَأَةٍ.
الْمَوَاضِيعُ: مَعْضَلَاتٌ يَوْمِيَّةٌ أَوْ مِهَنِيَّةٌ أَوْ تِقْنِيَّةٌ.
يَجِبُ أَنْ يَحْتَوِيَ كُلُّ حِوَارٍ عَلَى تَعْبِيرٍ اصْطِلَاحِيٍّ أَوْ نِيَّةٍ مُضْمَرَةٍ.
السُّؤَالُ لِكُلِّ حِوَارٍ: مَا نِيَّةُ الْمُتَكَلِّمِ الضِّمْنِيَّةُ أَوْ مَعْنَى التَّعْبِيرِ الِاصْطِلَاحِيِّ؟
""",
    },
    {
        "part": 2,
        "label": "الْحِوَارُ الْمُمْتَدُّ",
        "format": """
أَنْشِئْ حِوَارًا مُمْتَدًّا (15-20 سَطْرًا) بَيْنَ رَجُلٍ وَامْرَأَةٍ حَوْلَ جَدَلٍ اجْتِمَاعِيٍّ أَوْ تَعْلِيمِيٍّ.
6 أَسْئِلَةٍ تَخْتَبِرُ الْادِّعَاءَاتِ الْأَسَاسِيَّةَ وَالْحُجَجَ الْمُتَعَارِضَةَ.
""",
    },
    {
        "part": 3,
        "label": "الْخِطَابُ / الْمُحَاضَرَةُ",
        "format": """
أَنْشِئْ مُحَاضَرَةً أَكَادِيمِيَّةً مُسْتَمِرَّةً (20-25 سَطْرًا) فِي مَجَالٍ: عِلْمِيٍّ أَوْ بِيئِيٍّ أَوْ ثَقَافِيٍّ.
6 أَسْئِلَةٍ تَتَتَبَّعُ التَّسَلْسُلَ الْهَيْكَلِيَّ وَالْخَاتِمَةَ.
""",
    },
]

LISTENING_PROMPT = """
{format}

أَرْجِعِ النَّتِيجَةَ بِصِيغَةِ JSON فَقَطْ:
{{
  "transcript": "النَّصُّ الْكَامِلُ (أَوِ الْحِوَارَاتُ الْمُنْفَصِلَةُ إِنْ كَانَتِ الْجُزْءُ الأَوَّلَ)",
  "questions": [
    {{
      "q": "السُّؤَالُ",
      "A": "أ", "B": "ب", "C": "ج", "D": "د",
      "correct": "A",
      "explanation_uz": "Tushuntirish o'zbekcha..."
    }}
  ]
}}

يَجِبُ أَنْ تَكُونَ الأَسْئِلَةُ 6 بِالضَّبْطِ.
"""


async def listening_generate_part(part_info: dict) -> dict:
    prompt = LISTENING_PROMPT.format(format=part_info["format"])
    raw = await gemini_text(prompt, system=LISTENING_SYSTEM)
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


async def listening_send_part(update_or_query, context, session: dict):
    part_idx = session["li_part"]
    part_info = LISTENING_PARTS[part_idx]
    await _send_typing(update_or_query, context)

    try:
        part_data = await listening_generate_part(part_info)
    except Exception as e:
        logger.error(f"Listening generation error: {e}")
        await _reply(update_or_query, "⚠️ Xatolik. /start bilan qaytadan boshlang.")
        return

    session["li_part_data"] = part_data
    session["li_q_idx"] = 0
    session["state"] = ST_LI_ACTIVE

    transcript = part_data.get("transcript", "")
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎧 *{part_info['label']}* — الْجُزْءُ {part_idx + 1} مِنْ 3\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📻 *النَّصُّ الصَّوْتِيُّ (اقرأه كأنك تسمعه):*\n\n"
        f"{transcript}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"اقْرَأِ النَّصَّ بِعِنَايَةٍ وَكَأَنَّكَ تَسْمَعُهُ، ثُمَّ أَجِبْ."
    )
    kb = next_kb("▶️ بَدْءُ الأَسْئِلَةِ", "li_start_questions")
    await _reply(update_or_query, text, kb=kb, parse_mode="Markdown")


async def listening_send_question(update_or_query, context, session: dict):
    part_data = session["li_part_data"]
    part_idx = session["li_part"]
    q_idx = session["li_q_idx"]
    questions = part_data.get("questions", [])

    if q_idx >= len(questions):
        if part_idx < 2:
            session["li_part"] = part_idx + 1
            session["li_q_idx"] = 0
            kb = next_kb(f"▶️ الْجُزْءُ {part_idx + 2}", "li_next_part")
            await _reply(
                update_or_query,
                f"✅ *الْجُزْءُ {part_idx + 1} اكْتَمَلَ!*",
                kb=kb, parse_mode="Markdown"
            )
        else:
            session["state"] = ST_LI_DONE
            score = session.get("li_score", 0)
            pct = round((score / 18) * 100)
            text = (
                f"🏁 *اكْتَمَلَ اخْتِبَارُ الِاسْتِمَاعِ!*\n\n"
                f"النَّتِيجَةُ: {score}/18 ({pct}%)\n"
            )
            if pct >= 78:
                text += "📊 Daraja: B2–C1"
            elif pct >= 56:
                text += "📊 Daraja: B1–B2"
            else:
                text += "📊 Daraja: A1–B1"
            await _reply(update_or_query, text, kb=back_to_menu_kb(), parse_mode="Markdown")
        return

    q = questions[q_idx]
    # Repeat short transcript snippet
    transcript = part_data.get("transcript", "")
    transcript_short = transcript[:300] + "..." if len(transcript) > 300 else transcript

    text = (
        f"🎧 *مُقْتَطَفٌ مِنَ النَّصِّ:*\n_{transcript_short}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ *السُّؤَالُ {q_idx + 1}:*\n\n"
        f"{q['q']}\n\n"
        f"أ — {q['A']}\nب — {q['B']}\nج — {q['C']}\nد — {q['D']}"
    )
    kb = abcd_kb(q_idx)
    await _reply(update_or_query, text, kb=kb, parse_mode="Markdown")


async def listening_handle_answer(query, context, session: dict, chosen: str):
    await query.answer()
    part_data = session["li_part_data"]
    q_idx = session["li_q_idx"]
    q = part_data["questions"][q_idx]
    correct = q.get("correct", "A")

    if chosen == correct:
        session["li_score"] = session.get("li_score", 0) + 1
        icon, label = "✅", "To'g'ri!"
    else:
        icon = "❌"
        label = f"Noto'g'ri. To'g'ri javob: *{correct}* — {q.get(correct, '')}"

    feedback = f"{icon} *{label}*\n\n📝 *Izoh:*\n{q.get('explanation_uz', '')}"
    session["li_q_idx"] = q_idx + 1
    kb = next_kb("التَّالِي ←", "li_next_q")
    await query.edit_message_text(feedback, parse_mode="Markdown", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════
# MODULE 4 — SPEAKING  (مهارة التحدث)
# ══════════════════════════════════════════════════════════════════════
SPEAKING_PROMPTS = [
    {
        "num": 1, "time": "30 soniya", "level": "🟢 Boshlang'ich",
        "prompt_ar": "عَرِّفْ بِنَفْسِكَ: اذْكُرِ اسْمَكَ وَمَدِينَتَكَ وَهِوَايَتَكَ الْمُفَضَّلَةَ.",
        "prompt_uz": "O'zingizni tanishtiring: ismingiz, shahringiz va sevimli hobbiingiz haqida gapiring.",
    },
    {
        "num": 2, "time": "30 soniya", "level": "🟢 Boshlang'ich",
        "prompt_ar": "صِفِ الرُّوتِينَ الْيَوْمِيَّ لَكَ مِنِ الاسْتِيقَاظِ حَتَّى النَّوْمِ.",
        "prompt_uz": "Uyg'onishdan yotishgacha bo'lgan kundalik tartibingizni tasvirlab bering.",
    },
    {
        "num": 3, "time": "45 soniya", "level": "🟡 O'rta",
        "prompt_ar": "تَحَدَّثْ عَنْ أَهَمِّيَّةِ تَعَلُّمِ اللُّغَاتِ الأَجْنَبِيَّةِ فِي الْعَالَمِ الْحَدِيثِ.",
        "prompt_uz": "Zamonaviy dunyoda chet tillarini o'rganishning ahamiyati haqida gapiring.",
    },
    {
        "num": 4, "time": "45 soniya", "level": "🟡 O'rta",
        "prompt_ar": "مَا رَأْيُكَ فِي اسْتِخْدَامِ التِّكْنُولُوجِيَا فِي التَّعْلِيمِ؟ اذْكُرْ إِيجَابِيَّاتٍ وَسَلْبِيَّاتٍ.",
        "prompt_uz": "Ta'limda texnologiyadan foydalanish haqidagi fikringiz? Ijobiy va salbiy tomonlarini ayting.",
    },
    {
        "num": 5, "time": "60 soniya", "level": "🔴 Ilg'or",
        "prompt_ar": "نَاقِشْ مُشْكِلَةَ التَّغَيُّرِ الْمَنَاخِيِّ: أَسْبَابُهَا وَآثَارُهَا وَالْحُلُولُ الْمُقْتَرَحَةُ.",
        "prompt_uz": "Iqlim o'zgarishi muammosini muhokama qiling: sabablari, oqibatlari va taklif etilayotgan yechimlar.",
    },
    {
        "num": 6, "time": "60 soniya", "level": "🔴 Ilg'or",
        "prompt_ar": "قَارِنْ بَيْنَ التَّعْلِيمِ التَّقْلِيدِيِّ وَالتَّعْلِيمِ عَنْ بُعْدٍ مِنْ حَيْثُ الْفَعَّالِيَّةُ وَالنَّتَائِجُ.",
        "prompt_uz": "An'anaviy ta'lim va masofaviy ta'limni samaradorlik va natijalar jihatidan solishtiring.",
    },
]

SPEAKING_EVAL_PROMPT = """
Quyidagi so'zli nutqni At-Tanal arabiy imtihoni mezonlari asosida qat'iy baholang.

Nutqning mazmuni (transkripsiya yoki tahlil):
{transcript}

Baholash savoli/mavzu (arabcha):
{prompt_ar}

Quyidagi 4 mezon bo'yicha FAQAT o'zbekcha batafsil tahlil bering.
Maqtov yoki ijobiy umumlashtirishdan BUTUNLAY saqlaning. Faqat xatolar va qat'iy mezoniy tahlil:

🎤 النطق السليم وإخراج الحروف
[Fonetika va artikulyatsiya aniqligi: harflar talaffuzi, tashkil, maqd va qasr xatolarini ro'yxatlang]

⏱ الطلاقة والانسيابية
[Sur'at va nutq uzluksizligi: to'liq tushib qolgan bo'laklar, keraksiz to'xtashlar, qaytarishlar]

⚖️ التراكيب النحوية والصرفية
[Morfosintaktik to'g'rilik: aniq grammatik xatolar arabcha misol bilan, keyin to'g'ri varianti]

❌ الأخطاء المرصودة وتصويبها
[Barcha aniqlangan og'zaki xatolar ro'yxati: Xato → To'g'ri variant (arabcha)]
"""

SPEAKING_TRANSCRIBE_PROMPT = """
Ushbu audio faylni tinglab, arabcha nutqni so'zma-so'z transkripsiya qiling.
Faqat transkripsiya matnini qaytaring — hech qanday izoh yoki qo'shimcha narsa qo'shmang.
Agar nutq arabcha bo'lmasa: [ARABCHA EMAS] deb yozing.
"""


async def speaking_send_prompt(update_or_query, context, session: dict):
    sp_num = session.get("sp_current", 1)
    sp_info = SPEAKING_PROMPTS[sp_num - 1]

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎤 *مَهَارَةُ التَّحَدُّثِ* — السُّؤَالُ {sp_num} مِنْ 6\n"
        f"{sp_info['level']} | ⏱ {sp_info['time']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *الْمَطْلُوبُ (arabcha):*\n{sp_info['prompt_ar']}\n\n"
        f"🗣 *O'zbekcha:*\n_{sp_info['prompt_uz']}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎙 Ovozli xabar yuboring ({sp_info['time']} davomida gapiring)"
    )
    session["state"] = ST_SP_PROMPT
    session["sp_info"] = sp_info
    await _reply(update_or_query, text, parse_mode="Markdown")


async def speaking_evaluate(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict):
    """Download voice, transcribe, evaluate."""
    voice = update.message.voice
    sp_info = session.get("sp_info", SPEAKING_PROMPTS[0])

    await update.message.reply_text("🎧 Ovozingiz qabul qilindi. Tahlil boshlanmoqda...")

    try:
        voice_file = await context.bot.get_file(voice.file_id)
        async with httpx.AsyncClient(timeout=30.0) as hclient:
            resp = await hclient.get(voice_file.file_path)
            audio_bytes = resp.content

        # Step 1: Transcribe
        transcript = await gemini_audio(audio_bytes, "audio/ogg", SPEAKING_TRANSCRIBE_PROMPT)

        if "ARABCHA EMAS" in transcript or "[ARABCHA EMAS]" in transcript:
            await update.message.reply_text(
                "⚠️ Arabcha nutq aniqlanmadi. Iltimos arabcha gapiring va qayta yuboring."
            )
            return

        await update.message.reply_text(
            f"📝 *Transkripsiya:*\n_{transcript}_",
            parse_mode="Markdown"
        )

        # Step 2: Evaluate
        eval_prompt = SPEAKING_EVAL_PROMPT.format(
            transcript=transcript,
            prompt_ar=sp_info["prompt_ar"],
        )
        evaluation = await gemini_text(eval_prompt)

        sp_num = session.get("sp_current", 1)
        header = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *نَتِيجَةُ التَّقْيِيمِ — السُّؤَالُ {sp_num}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        await update.message.reply_text(header + evaluation, parse_mode="Markdown")

        # Advance or finish
        if sp_num < 6:
            session["sp_current"] = sp_num + 1
            kb = next_kb("▶️ السُّؤَالُ التَّالِي", "sp_next")
            await update.message.reply_text(
                f"السُّؤَالُ {sp_num} مَكْتَمِلٌ. اضْغَطْ لِلِانْتِقَالِ.",
                reply_markup=kb
            )
        else:
            session["state"] = ST_SP_DONE
            await update.message.reply_text(
                "🏁 *اكْتَمَلَ اخْتِبَارُ التَّحَدُّثِ!*\nBarcha 6 savol baholandi.",
                parse_mode="Markdown",
                reply_markup=back_to_menu_kb()
            )

    except Exception as e:
        logger.error(f"Speaking eval error: {e}")
        await update.message.reply_text(
            f"⚠️ Xatolik yuz berdi: {str(e)[:100]}\nQayta urinib ko'ring."
        )


# ══════════════════════════════════════════════════════════════════════
# MODULE 5 — WRITING  (مهارة الكتابة)
# ══════════════════════════════════════════════════════════════════════
WRITING_ESSAYS = [
    {
        "num": 1,
        "level": "الْمُسْتَوَى الْبَسِيطُ",
        "level_uz": "🟢 Oddiy daraja",
        "time": "15 daqiqa",
        "target_words": 100,
        "theme_ar": "تَجَارِبُ شَخْصِيَّةٌ أَوْ تَقَالِيدُ مَحَلِّيَّةٌ أَوْ سَرْدٌ وَاضِحٌ",
        "theme_uz": "Shaxsiy tajribalar, mahalliy an'analar yoki aniq hikoya",
        "structure": "3 bosqichli tuzilma: Kirish (Muqaddima) + Asosiy qism (Matn) + Xulosa",
        "topic_ar": "اكْتُبْ عَنْ يَوْمٍ لَا تَنْسَاهُ فِي حَيَاتِكَ. مَاذَا حَدَثَ؟ وَكَيْفَ أَثَّرَ فِيكَ؟",
        "topic_uz": "Hayotingizda unutolmaydigan kun haqida yozing. Nima bo'ldi? U sizga qanday ta'sir qildi?",
    },
    {
        "num": 2,
        "level": "الْمُسْتَوَى الْمُتَوَسِّطُ",
        "level_uz": "🟡 O'rta daraja",
        "time": "20 daqiqa",
        "target_words": 150,
        "theme_ar": "مَوَاضِيعُ اجْتِمَاعِيَّةٌ وَتَعْلِيمِيَّةٌ بِأَسْئِلَةٍ فَرْعِيَّةٍ",
        "theme_uz": "Ijtimoiy/ta'limiy mavzular, aniq qo'shimcha savollar bilan",
        "structure": "Aniq paragraf bo'linishi va mantiqiy o'tishlar",
        "topic_ar": "مَا دَوْرُ الْأُسْرَةِ فِي تَشْكِيلِ شَخْصِيَّةِ الطِّفْلِ؟ هَلِ الْمَدْرَسَةُ تَسْتَطِيعُ أَنْ تَعْوِضَ غِيَابَ الدَّوْرِ الأُسَرِيِّ؟ وَكَيْفَ نُحَقِّقُ التَّوَازُنَ؟",
        "topic_uz": "Oilaning bolaning shaxsiyatini shakllantirishdagi roli nima? Maktab oilaviy rolning yo'qligini qoplay oladimi? Qanday muvozanat yaratish mumkin?",
    },
    {
        "num": 3,
        "level": "الْمُسْتَوَى الْمُتَقَدِّمُ",
        "level_uz": "🔴 Ilg'or daraja",
        "time": "30 daqiqa",
        "target_words": 200,
        "theme_ar": "حَلُّ الْمُشْكِلَاتِ الأَكَادِيمِيَّةِ: الْمُشْكِلَةُ وَأَسْبَابُهَا وَآثَارُهَا وَحُلُولُهَا",
        "theme_uz": "Muammo-sabab-oqibat-yechim tuzilmasi",
        "structure": "Ritorik izchillik va takrorlanishdan qochish",
        "topic_ar": "يَعَانِي كَثِيرٌ مِنَ الشَّبَابِ مِنْ إِدْمَانِ وَسَائِلِ التَّوَاصُلِ الِاجْتِمَاعِيِّ. حَلِّلِ الأَسْبَابَ وَالآثَارَ عَلَى الْمُجْتَمَعِ وَاقْتَرِحِ الْحُلُولَ الْمُنَاسِبَةَ.",
        "topic_uz": "Ko'plab yoshlar ijtimoiy tarmoqlarga qaram. Sabablari va jamiyatga ta'sirini tahlil qiling, mos yechimlar taklif eting.",
    },
]

WRITING_EVAL_PROMPT = """
Quyidagi arabcha inshoni At-Tanal al-Arabi imtihoni mezonlari asosida QATTIQ va AKADEMIK tarzda baholang.

Savol/mavzu:
{topic_ar}

Daraja: {level} — Mo'ljallangan so'zlar: {target_words}
Talab qilingan tuzilma: {structure}

Talabaning inshosi:
---
{essay}
---

Quyidagi 4 mezon bo'yicha FAQAT o'zbekcha batafsil tahlil bering.
Maqtov yoki umumiy rag'batlantirish TAQIQLANADI. Faqat qat'iy mezoniy tahlil:

🔍 التحليل النحوي والصرفي
[Morfosintaktik tahlil: aniq xatolar arabcha ko'rsatib, to'g'ri varianti bilan birga]

✏️ الأخطاء الإملائية وتصويبها
[Imlo xatolari: hamza, ta marbuta/ha, alif maqsura xatolari — har birini alohida]

🧩 التناسق وبناء الأفكار
[Tuzilma va g'oyalar: mantiqiy rivojlanish, paragraflar ketma-ketligi, leksik tanlovi]

✨ النسخة المصححة بالكامل
[Inshoning to'liq to'g'rilangan varianti to'liq tashkil (harakat) bilan arabcha]
"""

WRITING_TOPIC_PROMPT = """
أَنْشِئْ مَوْضُوعًا إِضَافِيًّا لِلتَّدَرُّبِ لِلْمُسْتَوَى: {level}
الطُّولُ: {target_words} كَلِمَةٍ تَقْرِيبًا
الْهَيْكَلُ الْمَطْلُوبُ: {structure}

أَرْجِعِ النَّتِيجَةَ بِصِيغَةِ JSON فَقَطْ:
{{
  "topic_ar": "نَصُّ الْمَوْضُوعِ بِالْعَرَبِيَّةِ الْمُشَكَّلَةِ",
  "topic_uz": "O'zbek tilidagi tarjima",
  "hints_uz": "Yozishdan oldin e'tiborga olinadigan maslahatlar o'zbekcha"
}}
"""


async def writing_send_essay_selection(update_or_query, context, session: dict):
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✍️ *مَهَارَةُ الْكِتَابَةِ*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "اخْتَرْ مُسْتَوَى الإِنْشَاءِ:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 الْمُسْتَوَى الْبَسِيطُ (~100 كَلِمَةٍ)", callback_data="wr_essay_1")],
        [InlineKeyboardButton("🟡 الْمُسْتَوَى الْمُتَوَسِّطُ (~150 كَلِمَةٍ)", callback_data="wr_essay_2")],
        [InlineKeyboardButton("🔴 الْمُسْتَوَى الْمُتَقَدِّمُ (~200 كَلِمَةٍ)", callback_data="wr_essay_3")],
    ])
    await _reply(update_or_query, text, kb=kb, parse_mode="Markdown")


async def writing_send_prompt(update_or_query, context, session: dict, essay_num: int):
    essay_info = WRITING_ESSAYS[essay_num - 1]
    session["wr_essay_info"] = essay_info
    session["state"] = ST_WR_PROMPT

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✍️ *{essay_info['level_uz']}*\n"
        f"⏱ Vaqt: {essay_info['time']} | 📏 {essay_info['target_words']} so'z\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Mavzu (arabcha):*\n{essay_info['topic_ar']}\n\n"
        f"🗣 *O'zbekcha:*\n_{essay_info['topic_uz']}_\n\n"
        f"🏗 *Kerakli tuzilma:* {essay_info['structure']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✏️ Inshongizni arabcha yozing va yuboring."
    )
    await _reply(update_or_query, text, parse_mode="Markdown")


async def writing_evaluate(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict):
    essay_text = update.message.text.strip()
    essay_info = session.get("wr_essay_info", WRITING_ESSAYS[0])

    if len(essay_text.split()) < 20:
        await update.message.reply_text(
            "⚠️ Inshongiz juda qisqa (kamida 20 so'z kerak). Iltimos to'liqroq yozing."
        )
        return

    await update.message.reply_text("⏳ Inshongiz baholanmoqda — biroz kuting...")

    try:
        eval_prompt = WRITING_EVAL_PROMPT.format(
            topic_ar=essay_info["topic_ar"],
            level=essay_info["level"],
            target_words=essay_info["target_words"],
            structure=essay_info["structure"],
            essay=essay_text,
        )
        evaluation = await gemini_text(eval_prompt)

        word_count = len(essay_text.split())
        header = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *نَتِيجَةُ تَقْيِيمِ الْكِتَابَةِ*\n"
            f"{essay_info['level_uz']} | So'zlar: {word_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        # Split if too long for Telegram (4096 char limit)
        full_text = header + evaluation
        if len(full_text) > 3800:
            await update.message.reply_text(header + evaluation[:3700] + "\n\n_(davomi...)_", parse_mode="Markdown")
            await update.message.reply_text(evaluation[3700:], parse_mode="Markdown", reply_markup=back_to_menu_kb())
        else:
            await update.message.reply_text(full_text, parse_mode="Markdown", reply_markup=back_to_menu_kb())

        session["state"] = ST_WR_DONE

    except Exception as e:
        logger.error(f"Writing eval error: {e}")
        await update.message.reply_text(f"⚠️ Baholashda xatolik: {str(e)[:100]}")


# ══════════════════════════════════════════════════════════════════════
# TELEGRAM SEND HELPERS
# ══════════════════════════════════════════════════════════════════════
async def _send_typing(update_or_query, context):
    try:
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.chat.send_action("typing")
        elif hasattr(update_or_query, "from_user"):
            await context.bot.send_chat_action(
                update_or_query.from_user.id, "typing"
            )
    except Exception:
        pass


async def _reply(update_or_query, text: str, kb=None, parse_mode: str = None):
    """Universal reply: works for Update and CallbackQuery alike."""
    kwargs = {}
    if kb:
        kwargs["reply_markup"] = kb
    if parse_mode:
        kwargs["parse_mode"] = parse_mode

    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, **kwargs)
    elif hasattr(update_or_query, "edit_message_text"):
        try:
            await update_or_query.edit_message_text(text, **kwargs)
        except Exception:
            await update_or_query.message.reply_text(text, **kwargs)
    else:
        logger.warning(f"_reply: unknown object type: {type(update_or_query)}")


# ══════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"state": ST_MENU}

    name = update.effective_user.first_name or "Talaba"
    text = (
        f"السَّلَامُ عَلَيْكُمْ، {name}! 👋\n\n"
        "أَهْلًا بِكَ فِي مُحَاكِي اخْتِبَارِ *التَّنَالِ الْعَرَبِيِّ* الرَّسْمِيِّ.\n\n"
        "🎯 *الْوَحَدَاتُ الْمُتَاحَةُ:*\n"
        "📘 الْقَوَاعِدُ وَالْمُفْرَدَاتُ — 30 سُؤَالًا\n"
        "📖 فَهْمُ الْمَقْرُوءِ — 18 سُؤَالًا (3 نُصُوصٍ)\n"
        "🎧 فَهْمُ الْمَسْمُوعِ — 18 سُؤَالًا (3 أَجْزَاءٍ)\n"
        "🎤 مَهَارَةُ التَّحَدُّثِ — 6 أَسْئِلَةٍ صَوْتِيَّةٍ\n"
        "✍️ مَهَارَةُ الْكِتَابَةِ — 3 إِنْشَاءَاتٍ\n\n"
        "اخْتَرِ الْوَحْدَةَ لِلْبَدْءِ:"
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *At-Tanal Arab imtihoni simulyatori — Qo'llanma*\n\n"
        "*/start* — Asosiy menyuga qaytish\n"
        "*/help* — Shu qo'llanma\n\n"
        "*5 ta modul:*\n"
        "1️⃣ Grammatika va lug'at — 30 savol (ABCD)\n"
        "2️⃣ O'qish tushunish — 18 savol, 3 matn\n"
        "3️⃣ Tinglash tushunish — 18 savol, 3 qism\n"
        "4️⃣ Og'zaki nutq — 6 savol (ovozli xabar)\n"
        "5️⃣ Yozma nutq — 3 insho (matn yuboring)\n\n"
        "*Baholash tili:* Kriteriyalar arabcha, tahlil o'zbekcha.\n"
        "*Muhim:* Bu simulyator rasmiy At-Tanal imtihoni emas."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


# ══════════════════════════════════════════════════════════════════════
# CALLBACK QUERY ROUTER
# ══════════════════════════════════════════════════════════════════════
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data

    if uid not in sessions:
        sessions[uid] = {"state": ST_MENU}
    session = sessions[uid]

    # ── GLOBAL: back to menu ──
    if data == "back_menu":
        await query.answer()
        sessions[uid] = {"state": ST_MENU}
        await query.edit_message_text(
            "اخْتَرِ الْوَحْدَةَ:", reply_markup=main_menu_kb()
        )
        return

    # ── MODULE SELECTION ──
    if data == "mod_grammar":
        await query.answer()
        sessions[uid] = {"state": ST_GR_ACTIVE, "gr_current": 1, "gr_score": 0}
        session = sessions[uid]
        await grammar_send_question(query, context, session)

    elif data == "mod_reading":
        await query.answer()
        sessions[uid] = {"state": ST_RD_PART, "rd_part": 0, "rd_score": 0}
        session = sessions[uid]
        await reading_send_passage(query, context, session)

    elif data == "mod_listening":
        await query.answer()
        sessions[uid] = {"state": ST_LI_ACTIVE, "li_part": 0, "li_score": 0}
        session = sessions[uid]
        await listening_send_part(query, context, session)

    elif data == "mod_speaking":
        await query.answer()
        sessions[uid] = {"state": ST_SP_PROMPT, "sp_current": 1}
        session = sessions[uid]
        await speaking_send_prompt(query, context, session)

    elif data == "mod_writing":
        await query.answer()
        sessions[uid] = {"state": ST_MENU}
        session = sessions[uid]
        await writing_send_essay_selection(query, context, session)

    # ── GRAMMAR ANSWERS ──
    elif data.startswith("ans_") and session.get("state") == ST_GR_ACTIVE:
        parts = data.split("_")
        chosen = parts[-1]
        await grammar_handle_answer(query, context, session, chosen)

    elif data.startswith("gr_next_"):
        await query.answer()
        await grammar_send_question(query, context, session)

    # ── READING ──
    elif data == "rd_start_questions":
        await query.answer()
        await reading_send_question(query, context, session)

    elif data == "rd_next_q":
        await query.answer()
        await reading_send_question(query, context, session)

    elif data == "rd_next_part":
        await query.answer()
        await reading_send_passage(query, context, session)

    elif data.startswith("ans_") and session.get("state") == ST_RD_ACTIVE:
        parts = data.split("_")
        chosen = parts[-1]
        await reading_handle_answer(query, context, session, chosen)

    # ── LISTENING ──
    elif data == "li_start_questions":
        await query.answer()
        await listening_send_question(query, context, session)

    elif data == "li_next_q":
        await query.answer()
        await listening_send_question(query, context, session)

    elif data == "li_next_part":
        await query.answer()
        await listening_send_part(query, context, session)

    elif data.startswith("ans_") and session.get("state") == ST_LI_ACTIVE:
        parts = data.split("_")
        chosen = parts[-1]
        await listening_handle_answer(query, context, session, chosen)

    # ── SPEAKING ──
    elif data == "sp_next":
        await query.answer()
        await speaking_send_prompt(query, context, session)

    # ── WRITING ──
    elif data.startswith("wr_essay_"):
        await query.answer()
        essay_num = int(data.split("_")[-1])
        await writing_send_prompt(query, context, session, essay_num)

    else:
        await query.answer("⚠️ Noma'lum tugma.")


# ══════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER (text + voice routing)
# ══════════════════════════════════════════════════════════════════════
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in sessions:
        await cmd_start(update, context)
        return

    session = sessions[uid]
    state = session.get("state", ST_IDLE)

    if state == ST_WR_PROMPT:
        await writing_evaluate(update, context, session)
    else:
        await update.message.reply_text(
            "الرَّجَاءُ اخْتَرْ وَحْدَةً مِنَ الْقَائِمَةِ.",
            reply_markup=main_menu_kb()
        )


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in sessions:
        await cmd_start(update, context)
        return

    session = sessions[uid]
    state = session.get("state", ST_IDLE)

    if state == ST_SP_PROMPT:
        await speaking_evaluate(update, context, session)
    else:
        await update.message.reply_text(
            "🎤 Ovozli xabar faqat *Og'zaki nutq moduli* uchun qabul qilinadi.\n"
            "Iltimos modulni menyudan tanlang.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )


# ══════════════════════════════════════════════════════════════════════
# CALLBACK QUERY ANSWER ROUTING FIX
# (ans_ callbacks need state-aware routing — fixed dispatcher below)
# ══════════════════════════════════════════════════════════════════════
async def handle_callback_safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrap handle_callback with error handling."""
    try:
        await handle_callback(update, context)
    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)
        try:
            await update.callback_query.answer("⚠️ Xatolik yuz berdi.")
            await update.callback_query.message.reply_text(
                f"⚠️ Xatolik: {str(e)[:150]}\n\n/start bilan qaytadan boshlang.",
                reply_markup=main_menu_kb()
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
def main():
    logger.info("🚀 At-Tanal Arabic Bot starting...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Callbacks (all inline buttons)
    app.add_handler(CallbackQueryHandler(handle_callback_safe))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("✅ Bot is live — polling for updates.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
