import os
import json
import re
import fitz  # PyMuPDF
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from google import genai
from google.genai.errors import APIError

# --- 1️⃣ الإعدادات والمفاتيح ---
API_ID = 32209311          
API_HASH = "861307a3d66097419a7fb08e50ebcf9b"
BOT_TOKEN = "8822964470:AAE0bhT1ncar7lhuazGwhKsLUBZHScD1Bb8"

# 🔑 ضع مفتاح Gemini الصحيح هنا
GEMINI_API_KEY = "AQ.Ab8RN6LZL1ZhEQ9BFaVebN3-V0GaoLh9zlKuTeEDm0VrfryeVw" 

app = Client("quiz_maker_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

try:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    ai_client = None
    print("⚠️ تنبيه: مفتاح Gemini غير صحيح!")

user_states = {}

# --- 2️⃣ رسالة الترحيب ---
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_name = message.from_user.first_name
    await message.reply_text(
        f"أهلاً بك يا {user_name}! 🌟\n\nأنا بوت الاختبارات الذكي. أرسل لي ملف PDF وسأقوم بتحويله إلى اختبار ممتع وتفاعلي! 🚀🔥\n\n⏳ **ملاحظة:** سيكون بين كل سؤال وسؤال 30 ثانية للتفكير."
    )

# --- 3️⃣ معالجة الملف ---
@app.on_message(filters.document & filters.private)
async def handle_pdf(client, message):
    if not message.document.file_name.lower().endswith('.pdf'):
        await message.reply_text("❌ يرجى إرسال ملف بصيغة PDF فقط!")
        return

    waiting_msg = await message.reply_text("⏳ جاري قراءة الملف وتصميم الاختبار...")

    try:
        pdf_path = await message.download()
        text_content = ""
        doc = fitz.open(pdf_path)
        for page in doc[:15]:  
            text_content += page.get_text()
        doc.close()
        
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        if len(text_content.strip()) < 50:
            await waiting_msg.edit_text("❌ الملف فارغ أو عبارة عن صور فقط!")
            return

        prompt = (
            "اقرأ النص التالي واصنع منه اختباراً شاملاً من نوع خيارات (MCQ) باللغة العربية.\n"
            "يجب أن تكون الإجابة بصيغة JSON فقط كقائمة من الكائنات، بدون أي كلام جانبي.\n"
            "الهيكل المطلوب:\n"
            '[{"question": "نص السؤال", "options": ["خيار 1", "خيار 2", "خيار 3", "خيار 4"], "correct_index": 0, "explanation": "توضيح"}]\n\n'
            f"النص:\n{text_content}"
        )

        try:
            response = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
        except APIError:
            await waiting_msg.edit_text("⚠️ سيرفرات جوجل مضغوطة حالياً، حاول مجدداً بعد قليل.")
            return

        clean_text = response.text.strip()
        clean_text = re.sub(r'^```json\s*', '', clean_text)
        clean_text = re.sub(r'\s*```$', '', clean_text)
        
        try:
            quiz_data = json.loads(clean_text)
        except json.JSONDecodeError:
            await waiting_msg.edit_text("❌ حدث خطأ في تنسيق البيانات، أعد المحاولة.")
            return

        await waiting_msg.delete()

        user_states[message.chat.id] = {
            "questions": quiz_data,
            "current_index": 0,
            "score": 0
        }

        await send_next_question(client, message.chat.id)

    except Exception as e:
        print(f"Error: {e}")
        await message.reply_text("❌ حصلت لخبطة أثناء توليد الأسئلة، يرجى إرسال الملف مرة أخرى.")

# --- 4️⃣ دالة إرسال السؤال التالي ---
async def send_next_question(client, chat_id):
    state = user_states.get(chat_id)
    if not state:
        return

    idx = state["current_index"]
    questions = state["questions"]

    # 🏁 نهاية الاختبار
    if idx >= len(questions):
        score = state["score"]
        total = len(questions)
        await client.send_message(
            chat_id, 
            f"🏁 **خلصت الاختبار ياشطور!** 🎉\n\n"
            f"🎯 الدرجة النهائية: **{score} من {total}**\n\n"
            f"**كفوووو 👏🏻👏🏻👏🏻🌟🌟🌟🌼🌼🌼**"
        )
        del user_states[chat_id]
        return

    # 🌟 محطات تشجيع (يتم إرسالها مباشرة بدون تأخير 30 ثانية، لأنها مجرد تشجيع)
    if idx > 0 and idx % 10 == 0:
        station_num = idx // 10
        msgs = {
            1: "✨ **رهييب وربي! أول 10 أسئلة خلفنا!** استمر بالتألق 👏🌼🌟",
            2: "⭐ **كفو عليك! 20 سؤالاً بإصرار!** احسنتتتء! 🌟🌼👏",
            3: "🔥 **مستوى خارق! 30 سؤالاً!** استمروا في الاكتساح! 👏🌟🌼"
        }
        motivate_text = msgs.get(station_num, "🚀 **تستحق القمه!** إنجاز مذهل! 🌼🌟👏")
        continue_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("إكمال الاختبار 🚀", callback_data="continue_quiz")]
        ])
        await client.send_message(chat_id, motivate_text, reply_markup=continue_btn)
        return

    # عرض السؤال الحالي
    q = questions[idx]
    
    buttons = []
    emojis = ["🔵", "🟢", "🔴", "🟡"] 
    for i, option in enumerate(q["options"]):
        button_text = f"{emojis[i % len(emojis)]} {option}"
        buttons.append([InlineKeyboardButton(button_text, callback_data=f"ans_{i}")])

    text = f"📝 **السؤال رقم {idx + 1}/{len(questions)}**\n\n❓ {q['question']}"
    
    await client.send_message(
        chat_id, 
        text, 
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- 5️⃣ استقبال ضغطات الأزرار ---
@app.on_callback_query()
async def handle_callback(client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    state = user_states.get(chat_id)
    
    if not state:
        await callback_query.answer("انتهى هذا الاختبار.")
        return

    # زر إكمال الاختبار في محطات التشجيع
    if callback_query.data == "continue_quiz":
        await callback_query.message.delete()
        await send_next_question(client, chat_id)
        return

    idx = state["current_index"]
    questions = state["questions"]
    q = questions[idx]

    chosen_index = int(callback_query.data.split("_")[1])
    correct_index = int(q["correct_index"])

    # معالجة الإجابة
    if chosen_index == correct_index:
        state["score"] += 1
        result_text = "✅ **إجابة صحيحة!** 🎉"
        
        # 🌼 ورد صفراء تظهر وتختفي
        try:
            celebrate_msg = await client.send_message(
                chat_id,
                "🌼🌼🌼🌼🌼 **إجابة رائعة! واصل التألق!** 🌼🌼🌼🌼🌼"
            )
            await asyncio.sleep(2.5)
            await celebrate_msg.delete()
        except Exception:
            pass
    else:
        correct_option_text = q["options"][correct_index]
        result_text = f"❌ **إجابة خاطئة!**\n\n💡 الإجابة الصحيحة: **{correct_option_text}**"

    # إضافة الشرح
    if q.get("explanation"):
        result_text += f"\n\nℹ️ **توضيح:** {q['explanation']}"

    # 👇 تعديل الرسالة: عرض النتيجة وإزالة الأزرار فوراً
    await callback_query.message.edit_text(
        f"📝 **السؤال رقم {idx + 1}/{len(questions)}**\n\n❓ {q['question']}\n\n{result_text}"
    )

    # زيادة رقم السؤال
    state["current_index"] += 1

    # ⏳ نظام التأخير 30 ثانية قبل السؤال التالي
    await client.send_message(
        chat_id, 
        f"⏳ يرجى الانتظار **30 ثانية** قبل السؤال التالي..."
    )
    
    await asyncio.sleep(30)  # هنا التأخير 30 ثانية

    # حذف رسالة الانتظار وإرسال السؤال الجديد
    await send_next_question(client, chat_id)

if __name__ == "__main__":
    print("🚀 صاروخ الاختبارات التفاعلية (مع المؤقت) انطلق!")
    if GEMINI_API_KEY == "YOUR_REAL_GEMINI_API_KEY_HERE":
        print("⚠️ تنبيه: لم يتم وضع مفتاح Gemini الصحيح بعد!")
    app.run()