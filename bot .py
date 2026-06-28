import os
import json
import asyncio
import logging
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import pdfplumber
from openai import OpenAI
from pdf2image import convert_from_bytes
import pytesseract
import nest_asyncio
# ================= الإعدادات الأساسية =================
TELEGRAM_TOKEN = ""
OPENAI_API_KEY = ""
client = OpenAI(api_key=OPENAI_API_KEY)

# ================= إعدادات قائمة الانتظار =================
pdf_queue = asyncio.Queue()
concurrency_limit = asyncio.Semaphore(1)

user_sessions = {}
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# (لنظام ويندوز فقط) إذا كنت تستخدم ويندوز، قم بتفعيل هذا السطر ووضع مسار tesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
# ================= دوال استخراج النص =================
def extract_text_from_pdf(pdf_file_obj):
    text = ""
    with pdfplumber.open(pdf_file_obj) as pdf:
        for page in pdf.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n\n"
    
    if not text.strip():
        logging.info("تفعيل OCR للصور الضوئية...")
        pdf_file_obj.seek(0)
        try:
            images = convert_from_bytes(pdf_file_obj.read())
            for image in images:
                ocr_text = pytesseract.image_to_string(image, lang='ara+eng')
                text += ocr_text + "\n\n"
        except Exception as e:
            logging.error(f"خطأ OCR: {e}")
            return ""
    return text

# ================= دالة توليد الأسئلة (170 سؤال) =================
def generate_quiz_questions(text_content):
    # نسمح للنص بحد أقصى 40000 حرف لاستخراج 170 سؤال (تكفي لكتاب صغير)
    if len(text_content) > 40000:
        text_content = text_content[:40000]

    prompt = f"""
    أنت مساعد تعليمي خبير. اقرأ النص التالي بعناية، وقم بإنشاء **170 سؤالاً** بالضبط (اختيار من متعدد) بناءً على المحتوى.
    إذا كان النص لا يكفي، أنشئ أكبر عدد ممكن حتى 170.
    يجب أن تكون الأسئلة دقيقة جداً وذكية.
    الرد بصيغة JSON فقط، لا تكتب أي كلام إضافي.
    صيغة JSON: 
    [
        {{"question": "نص السؤال؟", "options": ["خيار 1", "خيار 2", "خيار 3", "خيار 4"], "correct": 0}},
        {{"question": "نص السؤال الثاني؟", "options": ["خيار 1", "خيار 2", "خيار 3", "خيار 4"], "correct": 2}}
    ]
    (الحقل "correct" يمثل فهرس الإجابة الصحيحة من 0 إلى 3)

    النص:
    {text_content}
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "أنت مولد أسئلة دقيق، ترد بصيغة JSON فقط."}, {"role": "user", "content": prompt}],
            temperature=0.5
        )
        json_response = response.choices[0].message.content
        if "```json" in json_response:
            json_response = json_response.split("```json")[1].split("```")[0]
        elif "```" in json_response:
            json_response = json_response.split("```")[1].split("```")[0]
        return json.loads(json_response)
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        return None

# ================= معالج المهام الخلفي =================
async def worker():
    while True:
        job = await pdf_queue.get()
        user_id, update, context, pdf_bytes = job
        user_name = update.effective_user.first_name or "صديقي"
        
        async with concurrency_limit:
            try:
                await context.bot.send_message(chat_id=user_id, text=f"🔄 جاري معالجة ملفك يا **{user_name}**، فضلاً انتظر...")
                text = extract_text_from_pdf(pdf_bytes)
                
                if not text.strip():
                    await context.bot.send_message(chat_id=user_id, text="❌ فشل استخراج النص. تأكد أن الـ PDF يحتوي على نصوص.")
                    pdf_queue.task_done()
                    continue

                await context.bot.send_message(chat_id=user_id, text="🧠 جاري إنشاء 170 سؤالاً (أو أقل حسب محتوى الـ PDF)...")
                questions = generate_quiz_questions(text)

                if not questions or len(questions) == 0:
                    await context.bot.send_message(chat_id=user_id, text="❌ حدث خطأ أثناء توليد الأسئلة.")
                    pdf_queue.task_done()
                    continue

                total = len(questions)
                user_sessions[user_id] = {
                    "questions": questions,
                    "current_q": 0,
                    "score": 0,
                    "total": total,
                    "name": user_name,
                    "checkpoint_pending": False  # حالة التوقف/الاستمرار
                }
                
                await context.bot.send_message(chat_id=user_id, text=f"✅ مرحباً **{user_name}**! تم توليد **{total}** سؤالاً بدقة عالية. 🚀\n⚠️ ملاحظة: بعد كل 20 سؤالاً، سأسألك إذا كنت ترغب في إكمال البقية.")
                await send_question(context, user_id)

            except Exception as e:
                logging.error(f"Error processing job for user {user_id}: {e}")
                await context.bot.send_message(chat_id=user_id, text="❌ حدث خطأ غير متوقع.")
            pdf_queue.task_done()

# ================= معالج استقبال الملف =================
async def handle_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document: return
    user_name = update.effective_user.first_name or "صديقي"
    file_name = update.message.document.file_name
    if not file_name.lower().endswith('.pdf'):
        await update.message.reply_text("⚠️ يرجى رفع ملف بصيغة PDF فقط.")
        return

    queue_size = pdf_queue.qsize()
    await update.message.reply_text(
        f"📥 أهلاً بك يا **{user_name}**! تم استلام ملفك.\n"
        f"⏳ ** يتم تحليل الملف    : {queue_size}**\n"
        f"سيبدأ البوت بتحضير الاختبار  .   🌟"
    )
    try:
        file = await update.message.document.get_file()
        pdf_bytes = BytesIO()
        await file.download_to_memory(pdf_bytes)
        pdf_bytes.seek(0)
        await pdf_queue.put((update.effective_user.id, update, context, pdf_bytes))
    except Exception as e:
        logging.error(f"Upload error: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء تحميل الملف.")

# ================= دوال السؤال (مع نظام الـ Checkpoint) =================
async def send_question(context, user_id):
    session = user_sessions.get(user_id)
    if not session: return
    
    current_q = session["current_q"]
    total = session["total"]
    
    # لقد انتهى الاختبار (أو وصلنا إلى النهاية)
    if current_q >= total:
        await end_quiz(context, user_id)
        return

    # ✅ منطق "بعد كل 20 سؤالاً" (Checkpoint)
    # نفذ هذا الشرط إذا تجاوزنا السؤال 0، وكان الرقم يقبل القسمة على 20، ولم نطلب التوقف مسبقاً
    if current_q > 0 and current_q % 20 == 0 and not session.get("checkpoint_pending"):
        session["checkpoint_pending"] = True
        keyboard = [
            [
                InlineKeyboardButton("✅ متابعة الإكمال", callback_data="continue_quiz"),
                InlineKeyboardButton("⏹️ التوقف الآن", callback_data="stop_quiz")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🌟 **عظيم يا {session['name']}! لقد أجبت على {current_q} سؤالاً حتى الآن!**\n\n💪 استمر بنفس القوة والنشاط!\n\n🛑 هل تريد إكمال الأسئلة المتبقية أم ترغب في التوقف هنا وعرض نتيجتك؟",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return # لا نرسل السؤال التالي حتى يقرر المستخدم

    # إذا لم يكن هناك توقف، نرسل السؤال العادي
    q_data = session["questions"][current_q]
    options = q_data["options"]
    keyboard = [[InlineKeyboardButton(options[i], callback_data=f"ans_{i}")] for i in range(len(options))]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"🎯 **سؤال {current_q + 1}/{total}**\n\n{q_data['question']}"
    await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")

# ================= معالج التوقف أو الاستمرار =================
async def handle_quiz_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    
    if not session: return
    
    if query.data == "continue_quiz":
        session["checkpoint_pending"] = False
        await query.edit_message_text("🚀 **رائع! لنكمل المشوار!** 🌟")
        await send_question(context, user_id)
    elif query.data == "stop_quiz":
        session["checkpoint_pending"] = False
        await query.edit_message_text("⏹️ **حسناً! سيتم إنهاء الاختبار وعرض النتيجة الآن.**")
        await end_quiz(context, user_id)

# ================= معالج الإجابات =================
async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    if not session: return

    # لن نسمح بالإجابة إذا كان البوت ينتظر قرار "اكمل أم توقف"
    if session.get("checkpoint_pending"):
        await query.message.reply_text("⏳ يرجى اتخاذ قرار (متابعة أو توقف) قبل الإجابة على السؤال التالي.")
        return

    choice = int(query.data.split("_")[1])
    current_index = session["current_q"]
    is_correct = (choice == session["questions"][current_index]["correct"])
    
    if is_correct:
        session["score"] += 1
        await query.message.reply_text("✅ **إجابة صحيحة! أحسنت يا بطل! 🌟**")
    else:
        correct_option = session["questions"][current_index]["options"][session["questions"][current_index]["correct"]]
        await query.message.reply_text(f"❌ **للأسف، أخطأت!** الإجابة الصحيحة هي: **{correct_option}** 💪 لا بأس، حاول في القادم.")

    session["current_q"] += 1
    await send_question(context, user_id)

# ================= إنهاء الاختبار =================
async def end_quiz(context, user_id):
    session = user_sessions.pop(user_id, None)
    name = session["name"] if session else "صديقي"
    score = session["score"] if session else 0
    total = session["total"] if session else 0
    
    final_msg = (
        f"🏁 **ألف مبروك يا {name}! انتهى الاختبار!** 🎉\n\n"
        f"📊 نتيجتك النهائية: **{score} من {total}**\n\n"
        f"🌟 170  كفووو يارهيب خلصت سؤالاً  🌼\n"
        f"لقد كنت رائعاً، نتمنى لك التوفيق دائماً ❤️\n\n"
        f"إذا أردت اختباراً جديداً، ارفع ملف PDF آخر!"
    )
    await context.bot.send_message(chat_id=user_id, text=final_msg, parse_mode="Markdown")

# ================= الأمر الترحيبي =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "صديقي"
    await update.message.reply_text(
        f"🌟 **أهلاً وسهلاً بك يا {user_name} في بوت الاختبارات الذكي!** 🌟\n\n"
        f"🚀 ارفع ملف PDF (نص أو صور ضوئية) وسأستخرج منه **حتى 170 سؤالاً**.\n"
        f"📌 **ميزة جديدة:** بعد كل 20 سؤالاً، سأعطيك خيار **الاستمرار أو التوقف**.\n\n"
        f"🌼 بالتوفيق يا بطل! 🌼",
        parse_mode="Markdown"
    )

# ================= تشغيل البوت =================
async def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Document.FileExtension("pdf"), handle_pdf_upload))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern="^ans_"))
    application.add_handler(CallbackQueryHandler(handle_quiz_decision, pattern="^(continue_quiz|stop_quiz)$"))
    asyncio.create_task(worker())
    print("🚀 بوت 170 سؤالاً يعمل مع نظام التوقف/الاستمرار...")
    application.run_polling()

if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
