import os, json, logging, base64
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from groq import Groq
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
YOUR_CHAT_ID   = int(os.environ["YOUR_CHAT_ID"])
TIMEZONE       = pytz.timezone(os.environ.get("TZ", "Europe/Moscow"))

DATA_FILE = Path("data.json")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY)

TEXT_MODEL   = "llama-3.3-70b-versatile"   # для чата и тренировок
VISION_MODEL = "llama-3.2-11b-vision-preview"  # для анализа фото

# ─── СИСТЕМНЫЙ ПРОМПТ ─────────────────────────────────────────────────────────
SYSTEM = """Ты — Макс, личный тренер. Говоришь на русском, на "ты", честно и коротко.

ПРОФИЛЬ:
- Рост 172 см, вес 94 кг → цель 80 кг (рельеф без объёма, кроссфит-стиль)
- Бег сейчас 3-5 км → цель 10 км регулярно
- Площадка: турники + брусья на улице, велик 2.5 км туда/обратно
- Зал: жим лёжа и база
- Слабое место: самодисциплина, риск перегорания
- Подтягивания даются тяжело, брусья — легко

ПРИНЦИПЫ:
1. Прогрессия бега: каждые 1-2 недели +0.5-1 км
2. Чередуй тяжёлые и лёгкие дни
3. Учись на данных: когда выходит, как оценивает, что пропускает
4. Мотивируй под его психологию — не шаблонно
5. Велик всегда туда И обратно — никогда "велик туда, бег обратно"
6. На площадке полноценные 40-60 мин: 4-5 упражнений × 4-5 подходов + финишёр

ФОРМАТ ТРЕНИРОВКИ — строго JSON без markdown:
{
  "type": "workout",
  "title": "название",
  "bike": true,
  "plan": [
    {"exercise": "Название", "sets": 4, "reps": "12", "note": "подсказка"}
  ],
  "motivation": "фраза",
  "estimated_time": "50 мин",
  "difficulty_target": 3
}

АНАЛИЗ ФОТО ПРОБЕЖКИ — строго JSON без markdown:
{
  "type": "run_result",
  "distance_km": 5.2,
  "duration": "28:14",
  "pace_avg": "5:26/км",
  "start_time": "07:15",
  "finish_time": "07:43",
  "calories": 420,
  "app": "Strava",
  "insight": "вывод о прогрессе и психологии"
}

Для обычных сообщений — просто текст."""

# ─── ДАННЫЕ ───────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {
        "chat_history": [],
        "runs": [],
        "weekly_stats": {},
        "pending_workout": None,
        "last_run_km": 3.0,
        "streak": 0,
        "last_workout_date": None,
        "psychology": {
            "all_start_times": [],
            "preferred_time": None,
            "avg_rating": [],
            "skip_patterns": [],
        }
    }

def save_data(d: dict):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def get_week_key() -> str:
    today = datetime.now(TIMEZONE)
    return (today - timedelta(days=today.weekday())).strftime("%Y-%W")

def build_context(data: dict) -> str:
    runs = data.get("runs", [])
    psych = data.get("psychology", {})
    w = data.get("weekly_stats", {}).get(get_week_key(), {})
    parts = [f"[Данные: бег {data['last_run_km']} км, серия {data.get('streak',0)} дней"]
    if runs:
        r = runs[-1]
        parts.append(f"| последняя пробежка: {r.get('distance_km')} км темп {r.get('pace_avg')} старт {r.get('start_time','?')}")
    if psych.get("preferred_time"):
        parts.append(f"| выходит обычно: {psych['preferred_time']}")
    if psych.get("avg_rating"):
        avg = sum(psych["avg_rating"][-5:]) / len(psych["avg_rating"][-5:])
        parts.append(f"| средняя оценка: {avg:.1f}/5")
    if w:
        parts.append(f"| неделя: {w.get('done',0)} тренировок {w.get('total_km',0):.1f} км")
    parts.append("]")
    return " ".join(parts)

# ─── GROQ API ─────────────────────────────────────────────────────────────────
def ask_groq(data: dict, user_message: str, image_bytes: bytes = None) -> str:
    ctx = build_context(data)

    if image_bytes:
        # Vision — анализ фото
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"{ctx}\nПользователь прислал скрин тренировки. Извлеки все данные и ответь ТОЛЬКО чистым JSON run_result."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            max_tokens=800,
            temperature=0.3
        )
    else:
        # Текстовый чат — берём последние 30 сообщений
        history = data.get("chat_history", [])[-30:]
        messages = (
            [{"role": "system", "content": SYSTEM}]
            + history
            + [{"role": "user", "content": f"{ctx}\n{user_message}"}]
        )
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            max_tokens=1200,
            temperature=0.7
        )

    reply = resp.choices[0].message.content.strip()

    # Сохраняем историю
    history = data.get("chat_history", [])
    history.append({"role": "user", "content": user_message or "📷 скрин"})
    history.append({"role": "assistant", "content": reply})
    data["chat_history"] = history[-40:]
    save_data(data)
    return reply

def parse_json(text: str) -> dict | None:
    try:
        clean = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        s = clean.find("{"); e = clean.rfind("}") + 1
        if s >= 0 and e > s:
            return json.loads(clean[s:e])
    except:
        pass
    return None

def fmt_workout(w: dict) -> str:
    em = "🚴" if w.get("bike") else "🏃"
    lines = [f"{em} *{w['title']}*", f"⏱ {w.get('estimated_time','?')}"]
    if w.get("bike"):
        lines.append("_🚲 Велик туда и велик обратно_\n")
    else:
        lines.append("")
    for i, ex in enumerate(w.get("plan", []), 1):
        s, r = ex["sets"], ex["reps"]
        line = f"{i}. {ex['exercise']} — " + (str(r) if s == 1 else f"{s}×{r}")
        if ex.get("note"):
            line += f"\n   _↳ {ex['note']}_"
        lines.append(line)
    if w.get("motivation"):
        lines.append(f"\n💬 _{w['motivation']}_")
    if w.get("difficulty_target"):
        lines.append(f"🎯 _Целевая сложность: {w['difficulty_target']}/5_")
    return "\n".join(lines)

def fmt_run(r: dict) -> str:
    lines = ["🏃 *Пробежка зафиксирована!*\n"]
    if r.get("distance_km"): lines.append(f"📏 Дистанция: *{r['distance_km']} км*")
    if r.get("duration"):    lines.append(f"⏱ Время: *{r['duration']}*")
    if r.get("pace_avg"):    lines.append(f"⚡ Темп: *{r['pace_avg']}*")
    if r.get("start_time"):  lines.append(f"🕐 Старт: {r['start_time']}")
    if r.get("finish_time"): lines.append(f"🏁 Финиш: {r['finish_time']}")
    if r.get("calories"):    lines.append(f"🔥 Калории: ~{r['calories']} ккал")
    if r.get("app"):         lines.append(f"📱 {r['app']}")
    if r.get("insight"):     lines.append(f"\n💡 _{r['insight']}_")
    return "\n".join(lines)

def accept_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принимаю!", callback_data="accept"),
        InlineKeyboardButton("💬 Изменить",  callback_data="suggest"),
        InlineKeyboardButton("😴 Пропустить",callback_data="skip_today"),
    ]])

def rating_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("1 — вышел",  callback_data="rate_1"),
        InlineKeyboardButton("2 — легко",  callback_data="rate_2"),
        InlineKeyboardButton("3 — норм",   callback_data="rate_3"),
        InlineKeyboardButton("4 — огонь",  callback_data="rate_4"),
        InlineKeyboardButton("5 — убит",   callback_data="rate_5"),
    ]])

def update_streak(data: dict):
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
    last = data.get("last_workout_date")
    data["streak"] = (data.get("streak", 0) + 1) if last in (yesterday, today) else 1
    data["last_workout_date"] = today

# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 *Привет! Я Макс — твой личный тренер.*\n\n"
        "Чем больше данных мне даёшь — тем точнее я под тебя подстраиваюсь.\n\n"
        "📷 Скинь скрин пробежки — зафиксирую всё автоматически\n"
        "🌙 В 20:00 — задание на завтра\n"
        "⏰ В 6:30 — подъём!\n"
        "📊 Воскресенье — анализ недели\n\n"
        "/workout — тренировка сейчас\n"
        "/done — выполнил\n"
        "/skip причина — пропустил\n"
        "/stats — статистика\n"
        "/profile — мой психопрофиль",
        parse_mode="Markdown"
    )

async def cmd_workout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    weekday = datetime.now(TIMEZONE).strftime("%A")
    weather = " ".join(ctx.args) if ctx.args else "ясно"
    msg = await update.message.reply_text("⏳ Составляю тренировку...")
    reply = ask_groq(data, f"Составь тренировку на сегодня ({weekday}), погода: {weather}. ТОЛЬКО JSON.")
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        data["pending_workout"] = w; save_data(data)
        await msg.edit_text(fmt_workout(w), parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await msg.edit_text(reply)

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    week = get_week_key()
    ws = data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    ws["done"] += 1
    update_streak(data)
    comment = " ".join(ctx.args) if ctx.args else ""
    reply = ask_groq(data, f"Тренировка выполнена! {comment} Похвали коротко и попроси оценку 1-5.")
    save_data(data)
    await update.message.reply_text(
        f"🎉 *Засчитано! Серия: {data['streak']} дней* 🔥\n\n{reply}",
        parse_mode="Markdown",
        reply_markup=rating_kb()
    )

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    week = get_week_key()
    data.setdefault("weekly_stats",{}).setdefault(week,{"done":0,"skipped":0,"total_km":0})["skipped"] += 1
    data["streak"] = 0
    reason = " ".join(ctx.args) if ctx.args else "без причины"
    data.setdefault("psychology",{}).setdefault("skip_patterns",[]).append(
        {"date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"), "reason": reason}
    )
    reply = ask_groq(data, f"Пропустил. Причина: {reason}. Пойми, не ругай, мотивируй на завтра. Учти его психологию.")
    save_data(data)
    await update.message.reply_text(reply)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    w = data.get("weekly_stats",{}).get(get_week_key(),{"done":0,"skipped":0,"total_km":0})
    runs = data.get("runs", [])
    best = max((r.get("distance_km",0) for r in runs), default=0)
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"*Эта неделя:*\n"
        f"✅ Тренировок: {w['done']}  ❌ Пропусков: {w['skipped']}\n"
        f"🏃 Набегано: {w['total_km']:.1f} км\n\n"
        f"*Всего:*\n"
        f"🔥 Серия: {data.get('streak',0)} дней\n"
        f"📏 Текущий бег: {data['last_run_km']} км\n"
        f"🏆 Лучшая пробежка: {best} км\n"
        f"📋 Всего пробежек: {len(runs)}",
        parse_mode="Markdown"
    )

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    psych = data.get("psychology", {})
    ratings = psych.get("avg_rating", [])
    skips = psych.get("skip_patterns", [])
    lines = ["🧠 *Психопрофиль*\n"]
    pref = psych.get("preferred_time")
    lines.append(f"⏰ Выходишь: {pref if pref else 'данных пока мало'}")
    if ratings:
        avg = sum(ratings[-10:]) / len(ratings[-10:])
        label = ("легко, можно добавлять" if avg < 2.5
                 else "на пределе, следи за восстановлением" if avg > 4
                 else "рабочая зона ✅")
        lines.append(f"💪 Средняя нагрузка: {avg:.1f}/5 — {label}")
    lines.append(f"📉 Пропусков: {len(skips)}")
    if skips:
        reasons = list({s.get('reason','') for s in skips[-5:]})
        lines.append(f"   _Причины: {', '.join(reasons)}_")
    lines.append(f"📋 Пробежек зафиксировано: {len(data.get('runs',[]))}")
    analysis = ask_groq(data, "Дай краткий психологический анализ: когда реально выходит, что мотивирует, как с ним работать. 3 предложения.")
    lines.append(f"\n💬 *Анализ:*\n{analysis}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── ФОТО (скрин пробежки) ────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    msg = await update.message.reply_text("📷 Читаю скрин...")
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = bytes(await file.download_as_bytearray())

    data = load_data()
    reply = ask_groq(data, "скрин тренировки", img_bytes)
    result = parse_json(reply)

    if result and result.get("type") == "run_result":
        record = {"timestamp": datetime.now(TIMEZONE).isoformat(), **{k:v for k,v in result.items() if k!="type"}}
        data.setdefault("runs", []).append(record)

        km = result.get("distance_km", 0)
        if km:
            data["last_run_km"] = km
            week = get_week_key()
            ws = data.setdefault("weekly_stats",{}).setdefault(week,{"done":0,"skipped":0,"total_km":0})
            ws["total_km"] += km
            ws["done"] += 1

        update_streak(data)

        # Психопрофиль — время выхода
        start = result.get("start_time","")
        if start:
            try:
                h = int(start.split(":")[0])
                label = ("раннее утро (до 9:00)" if h < 9
                         else "утро (9–12)" if h < 12
                         else "день (12–17)" if h < 17
                         else "вечер (после 17:00)")
                times = data.setdefault("psychology",{}).setdefault("all_start_times",[])
                times.append(label)
                data["psychology"]["preferred_time"] = max(set(times), key=times.count)
            except: pass

        save_data(data)
        await msg.edit_text(
            fmt_run(record) + "\n\n*Как оцениваешь сложность?*",
            parse_mode="Markdown",
            reply_markup=rating_kb()
        )
    else:
        await msg.edit_text(reply or "Не смог разобрать скрин. Попробуй другое фото.")

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────
RATE_TEXT = {
    1: "Просто вышел — уже молодец! Следующая такая же.",
    2: "Легко прошло. Накидываем чуть нагрузки.",
    3: "Рабочий режим! Добавим 0.5 км или подход.",
    4: "Огонь! Ты в своей зоне — держим темп.",
    5: "Мощно! Следующая — лёгкое восстановление."
}

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = load_data()

    if q.data.startswith("rate_"):
        n = int(q.data[-1])
        data.setdefault("psychology",{}).setdefault("avg_rating",[]).append(n)
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Оценка {n}/5 — {RATE_TEXT[n]}")

    elif q.data == "accept":
        reply = ask_groq(data, "Принял тренировку! Скажи мотивирующее, 1-2 предложения.")
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🔥 {reply}\n\nКогда закончишь — /done")

    elif q.data == "suggest":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("💬 Расскажи что хочешь изменить — скорректирую план!")

    elif q.data == "skip_today":
        data["streak"] = 0
        week = get_week_key()
        data.setdefault("weekly_stats",{}).setdefault(week,{"done":0,"skipped":0,"total_km":0})["skipped"] += 1
        reply = ask_groq(data, "Пропускает сегодня. Пойми, мотивируй на завтра. 1 предложение.")
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(reply)

# ─── ТЕКСТОВЫЙ ЧАТ ───────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    data = load_data()
    reply = ask_groq(data, update.message.text)
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        data["pending_workout"] = w; save_data(data)
        await update.message.reply_text(fmt_workout(w), parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await update.message.reply_text(reply)

# ─── ПЛАНИРОВЩИК ──────────────────────────────────────────────────────────────
async def job_wakeup(app: Application):
    data = load_data()
    w = data.get("pending_workout")
    text = (f"🌅 *6:30 — Подъём!* 💪\n\n{fmt_workout(w)}" if w
            else "🌅 *6:30 — Подъём!* Напиши /workout для задания!")
    await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown")

async def job_evening(app: Application):
    data = load_data()
    tomorrow = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%A")
    reply = ask_groq(data, f"Составь тренировку на завтра ({tomorrow}). Учти все данные. ТОЛЬКО JSON.")
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        data["pending_workout"] = w; save_data(data)
        await app.bot.send_message(
            YOUR_CHAT_ID,
            f"🌙 *Задание на завтра:*\n\n{fmt_workout(w)}",
            parse_mode="Markdown", reply_markup=accept_kb()
        )

async def job_motivation(app: Application):
    data = load_data()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    if data.get("last_workout_date") != today:
        reply = ask_groq(data, "Не тренировался сегодня. Подстегни — учти его психологию. 1-2 предложения.")
        await app.bot.send_message(YOUR_CHAT_ID, f"⚡ {reply}")

async def job_weekly(app: Application):
    data = load_data()
    w = data.get("weekly_stats",{}).get(get_week_key(),{"done":0,"skipped":0,"total_km":0})
    prompt = (
        f"Итоги недели: {w['done']} тренировок, {w['skipped']} пропусков, "
        f"{w['total_km']:.1f} км бега, серия {data.get('streak',0)} дней, "
        f"текущий бег {data['last_run_km']} км. "
        "Анализ недели (4-5 предложений): прогресс, паттерны, цель на следующую неделю. "
        "Учти психологию пользователя."
    )
    reply = ask_groq(data, prompt)
    await app.bot.send_message(
        YOUR_CHAT_ID,
        f"📊 *ИТОГИ НЕДЕЛИ*\n\n"
        f"✅ {w['done']} тренировок  ❌ {w['skipped']} пропусков\n"
        f"🏃 {w['total_km']:.1f} км  🔥 серия {data.get('streak',0)} дней\n\n{reply}",
        parse_mode="Markdown"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("workout", cmd_workout))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("skip",    cmd_skip))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_wakeup,     "cron", hour=6,  minute=30, args=[app])
    scheduler.add_job(job_motivation, "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(job_evening,    "cron", hour=20, minute=0,  args=[app])
    scheduler.add_job(job_weekly,     "cron", day_of_week="sun",  hour=19, args=[app])
    scheduler.start()

    log.info("✅ Бот Макс запущен на Groq!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
