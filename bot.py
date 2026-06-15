import os, json, logging, base64, urllib.request, urllib.parse
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

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY      = os.environ["GROQ_API_KEY"]
YOUR_CHAT_ID      = int(os.environ["YOUR_CHAT_ID"])
TIMEZONE          = pytz.timezone(os.environ.get("TZ", "Asia/Yekaterinburg"))
WEATHER_API_KEY   = os.environ.get("WEATHER_API_KEY", "")
CITY              = os.environ.get("CITY", "Yekaterinburg")

DATA_FILE = Path("data.json")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)
client = Groq(api_key=GROQ_API_KEY)

# ─── ПОГОДА ───────────────────────────────────────────────────────────────────
def get_weather(target_hour: int = 7) -> dict:
    """Получает погоду на конкретный час завтрашнего дня."""
    if not WEATHER_API_KEY:
        return {}
    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?q={urllib.parse.quote(CITY)}&appid={WEATHER_API_KEY}"
            f"&units=metric&lang=ru&cnt=16"
        )
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())

        tomorrow = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
        best = None
        best_diff = 999

        for item in data.get("list", []):
            dt_txt = item.get("dt_txt", "")
            if not dt_txt.startswith(tomorrow):
                continue
            hour = int(dt_txt[11:13])
            diff = abs(hour - target_hour)
            if diff < best_diff:
                best_diff = diff
                best = item

        if not best:
            return {}

        main   = best["main"]
        wind   = best.get("wind", {})
        rain   = best.get("rain", {}).get("3h", 0)
        snow   = best.get("snow", {}).get("3h", 0)
        desc   = best["weather"][0]["description"] if best.get("weather") else ""
        temp   = round(main.get("temp", 0))
        feels  = round(main.get("feels_like", 0))
        speed  = round(wind.get("speed", 0))

        is_bad = rain > 0.5 or snow > 0 or speed > 10
        emoji  = ("🌧" if rain > 0.5 else "❄️" if snow > 0
                  else "💨" if speed > 10 else "⛅" if "облач" in desc else "☀️")

        return {
            "temp": temp,
            "feels": feels,
            "desc": desc,
            "wind_speed": speed,
            "rain": rain,
            "snow": snow,
            "is_bad": is_bad,
            "emoji": emoji,
            "bike_ok": not is_bad,
            "summary": f"{emoji} {temp}°C, {desc}, ветер {speed} м/с" + (f", дождь {rain} мм" if rain else ""),
            "hour": f"{tomorrow} {target_hour:02d}:00"
        }
    except Exception as e:
        log.warning(f"Weather error: {e}")
        return {}

def fmt_weather(w: dict, context: str = "утро") -> str:
    if not w:
        return ""
    lines = [f"\n🌤 *Погода на {context}:* {w['summary']}"]
    if w.get("feels") and abs(w["feels"] - w["temp"]) > 2:
        lines.append(f"   _↳ Ощущается как {w['feels']}°C_")
    if w.get("is_bad"):
        lines.append("   _↳ Велик не берём — плохая погода_")
    else:
        lines.append("   _↳ Погода норм — велик берём! 🚴_")
    return "\n".join(lines)

TEXT_MODEL   = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-11b-vision-preview"

SYSTEM = """Ты — Макс, личный тренер. Говоришь на русском, на "ты", кратко и по делу.

═══ ПРОФИЛЬ ═══
Рост 172 см, вес 94 кг → цель 80 кг (рельеф без объёма, кроссфит-стиль)
Бег: сейчас 3-5 км → цель 10 км регулярно
Площадка: воркаут на улице (турники, брусья)
Велосипед: 2.5 км от дома до площадки
Зал: жим лёжа и база
Проблемы: самодисциплина, риск перегорания
Подтягивания тяжело → начинаем с австралийских и негативов
Брусья — легко

═══ ТИПЫ ТРЕНИРОВОК (строго три варианта) ═══

1. ВЕЛИК + ПЛОЩАДКА:
   - Едет на велике до площадки (2.5 км = разминка)
   - Тренируется на площадке 40-60 минут (минимум 4 упражнения × 4 подхода + финишёр)
   - Едет на велике домой (2.5 км = заминка)
   - В этот день БЕГ НЕ ДОБАВЛЯЕМ — велик это уже кардио

2. ТОЛЬКО БЕГ:
   - Выходит из дома и бежит
   - Никакой площадки, никакого велика
   - Прогрессия: 3.5 → 4 → 5 → 6 → 7 → 8 → 10 км

3. ЗАЛ:
   - Жим лёжа, тяга, база
   - Без велика и бега

═══ ПРАВИЛА ═══
1. НИКОГДА не смешивай: велик туда + бег обратно — это абсурд
2. На площадке ПОЛНОЦЕННАЯ тренировка: 40-60 мин, 4-5 упр × 4-5 подходов, финишёр 10-15 мин
3. Финишёр всегда в конце: берпи, AMRAP, табата, интервалы
4. Прогрессия бега: +0.5 км каждые 1-2 недели
5. Чередуй: тяжёлый день → лёгкий день → отдых
6. Если спрашивает погоду — уточни перед тренировкой с велосипедом
7. Запоминай предпочтения: "не люблю берпи" → не давай берпи

═══ ФОРМАТЫ ОТВЕТОВ ═══

Когда предлагаешь тренировку — ТОЛЬКО этот JSON, без лишних слов до и после:
{"type":"workout","title":"название","bike":true,"plan":[{"exercise":"название","sets":4,"reps":"12","note":"подсказка"}],"motivation":"короткая фраза","estimated_time":"50 мин","difficulty_target":3}

Когда анализируешь фото пробежки — ТОЛЬКО этот JSON:
{"type":"run_result","distance_km":5.2,"duration":"28:14","pace_avg":"5:26/км","start_time":"07:15","finish_time":"07:43","calories":420,"app":"Strava","insight":"вывод о прогрессе"}

Когда анализируешь фото тренажёра или спортивного снаряда — ТОЛЬКО этот JSON:
{"type":"equipment","name":"Жим ногами","muscle_group":"Квадрицепс, ягодицы","how_to_use":"Ляг на спину, упри ноги в платформу, выжимай вес","sets_reps":"4×12","note":"Вес по ощущениям — последние 2 повтора должны даваться с усилием","can_add":true}

Для ВСЕХ остальных сообщений — обычный текст, никакого JSON.

═══ ЗАПРЕЩЕНО ═══
- Показывать JSON пользователю в обычном разговоре
- Говорить что пользователь уже побежал если он не подтвердил
- Смешивать велик и бег в одну тренировку
- Делать тренировку из 3 упражнений по 3 подхода — это слишком мало"""

def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {
        "chat_history": [],
        "runs": [],
        "workouts_done": [],
        "weekly_stats": {},
        "monthly_km": {},
        "weight_log": [{"date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"), "weight": 94}],
        "pending_workout": None,
        "workout_accepted": False,
        "last_run_km": 3.0,
        "streak": 0,
        "last_workout_date": None,
        "preferences": [],
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

def get_month_key() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m")

def build_context(data: dict) -> str:
    runs = data.get("runs", [])
    psych = data.get("psychology", {})
    w = data.get("weekly_stats", {}).get(get_week_key(), {})
    weight_log = data.get("weight_log", [])
    prefs = data.get("preferences", [])
    monthly = data.get("monthly_km", {}).get(get_month_key(), 0)

    parts = [f"[ДАННЫЕ ТРЕНЕРА:"]
    parts.append(f"бег {data['last_run_km']} км | серия {data.get('streak',0)} дней")

    if weight_log:
        last_w = weight_log[-1]
        parts.append(f"| вес {last_w.get('weight', 94)} кг (цель 80 кг, осталось {last_w.get('weight',94)-80} кг)")

    if runs:
        r = runs[-1]
        parts.append(f"| последний бег: {r.get('distance_km')} км темп {r.get('pace_avg','?')} старт {r.get('start_time','?')}")

    if w:
        parts.append(f"| эта неделя: {w.get('done',0)} тренировок {w.get('total_km',0):.1f} км бега")

    if monthly:
        parts.append(f"| этот месяц: {monthly:.1f} км")

    if psych.get("preferred_time"):
        parts.append(f"| обычно выходит: {psych['preferred_time']}")

    if psych.get("avg_rating"):
        avg = sum(psych["avg_rating"][-5:]) / len(psych["avg_rating"][-5:])
        parts.append(f"| средняя оценка: {avg:.1f}/5")

    if prefs:
        parts.append(f"| предпочтения: {'; '.join(prefs[-5:])}")

    parts.append("]")
    return " ".join(parts)

def ask_groq(data: dict, user_message: str, image_bytes: bytes = None) -> str:
    ctx = build_context(data)
    try:
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": f"{ctx}\nПосмотри на фото. Если это скрин из беговое/спортивного приложения — ответь JSON run_result. Если это фото тренажёра или спортивного снаряда на улице — ответь JSON equipment. Ответь ТОЛЬКО чистым JSON без markdown."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]}
            ]
            resp = client.chat.completions.create(model=VISION_MODEL, messages=messages, max_tokens=600, temperature=0.1)
        else:
            history = data.get("chat_history", [])[-30:]
            messages = (
                [{"role": "system", "content": SYSTEM}]
                + history
                + [{"role": "user", "content": f"{ctx}\n{user_message}"}]
            )
            resp = client.chat.completions.create(model=TEXT_MODEL, messages=messages, max_tokens=1000, temperature=0.7)

        reply = resp.choices[0].message.content.strip()
        # Убираем markdown обёртки если есть
        reply = reply.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        history = data.get("chat_history", [])
        history.append({"role": "user", "content": user_message or "📷 скрин"})
        history.append({"role": "assistant", "content": reply})
        data["chat_history"] = history[-40:]
        save_data(data)
        return reply

    except Exception as e:
        log.error(f"Groq error: {e}")
        return "Что-то пошло не так, попробуй ещё раз."

def parse_json(text: str) -> dict | None:
    try:
        s = text.find("{"); e = text.rfind("}") + 1
        if s >= 0 and e > s:
            return json.loads(text[s:e])
    except:
        pass
    return None

def fmt_workout(w: dict) -> str:
    em = "🚴" if w.get("bike") else "🏃"
    lines = [f"{em} *{w['title']}*", f"⏱ {w.get('estimated_time','?')}"]
    if w.get("bike"):
        lines.append("_🚲 Велик до площадки → тренировка → велик домой_\n")
    else:
        lines.append("")
    for i, ex in enumerate(w.get("plan", []), 1):
        s, r = ex.get("sets",1), ex.get("reps","")
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
    if r.get("calories"):    lines.append(f"🔥 ~{r['calories']} ккал")
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

def add_km(data: dict, km: float):
    week = get_week_key()
    month = get_month_key()
    data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    data["weekly_stats"][week]["total_km"] += km
    data.setdefault("monthly_km", {})[month] = data["monthly_km"].get(month, 0) + km
    data["last_run_km"] = km

def update_preferred_time(data: dict, start_time: str):
    if not start_time:
        return
    try:
        h = int(start_time.split(":")[0])
        label = ("раннее утро (до 9:00)" if h < 9
                 else "утро (9–12)" if h < 12
                 else "день (12–17)" if h < 17
                 else "вечер (после 17:00)")
        times = data.setdefault("psychology", {}).setdefault("all_start_times", [])
        times.append(label)
        data["psychology"]["preferred_time"] = max(set(times), key=times.count)
    except:
        pass

# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 *Привет! Я Макс — твой личный тренер.*\n\n"
        "Работаю 24/7, обучаюсь на твоих данных.\n\n"
        "📷 *Скинь скрин пробежки* — всё зафиксирую автоматически\n"
        "🌙 *20:00* — задание на завтра\n"
        "⏰ *6:30* — подъём!\n"
        "⚡ *14:00* — напомню если не тренировался\n"
        "📊 *Воскресенье* — итоги недели\n"
        "⚖️ *Понедельник* — спрошу вес\n\n"
        "Команды:\n"
        "/workout — тренировка прямо сейчас\n"
        "/workout дождь — с учётом погоды\n"
        "/done — отметить выполнение\n"
        "/skip причина — пропустить\n"
        "/weight 93 — записать вес\n"
        "/stats — статистика\n"
        "/profile — мой психопрофиль",
        parse_mode="Markdown"
    )

async def cmd_workout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    weekday = datetime.now(TIMEZONE).strftime("%A")
    weather_arg = " ".join(ctx.args) if ctx.args else ""
    weather = get_weather(target_hour=datetime.now(TIMEZONE).hour + 1) if not weather_arg else {}
    bike_ok = weather.get("bike_ok", True)
    weather_context = weather_arg or weather.get("summary", "ясно")
    msg = await update.message.reply_text("⏳ Составляю тренировку...")
    reply = ask_groq(data,
        f"Составь тренировку на сегодня ({weekday}). Погода: {weather_context}. "
        f"{'Велик можно.' if bike_ok else 'Велик НЕ берём — плохая погода.'} "
        "Ответь ТОЛЬКО чистым JSON workout."
    )
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        if not bike_ok:
            w["bike"] = False
        data["pending_workout"] = w
        data["workout_accepted"] = False
        save_data(data)
        weather_str = fmt_weather(weather, "сейчас") if weather else ""
        text = (weather_str + "\n\n" + fmt_workout(w)) if weather_str else fmt_workout(w)
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await msg.edit_text(reply)

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    week = get_week_key()
    data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    data["weekly_stats"][week]["done"] += 1
    update_streak(data)
    comment = " ".join(ctx.args) if ctx.args else ""
    reply = ask_groq(data, f"Тренировка выполнена! {comment} Похвали коротко (1-2 предложения) и попроси оценку 1-5.")
    save_data(data)
    await update.message.reply_text(
        f"🎉 *Засчитано! Серия: {data['streak']} дней* 🔥\n\n{reply}",
        parse_mode="Markdown",
        reply_markup=rating_kb()
    )

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    week = get_week_key()
    data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    data["weekly_stats"][week]["skipped"] += 1
    data["streak"] = 0
    reason = " ".join(ctx.args) if ctx.args else "без причины"
    data.setdefault("psychology", {}).setdefault("skip_patterns", []).append({
        "date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
        "reason": reason
    })
    reply = ask_groq(data, f"Пропустил тренировку. Причина: {reason}. Пойми, не ругай, 1-2 предложения мотивации на завтра.")
    save_data(data)
    await update.message.reply_text(reply)

async def cmd_weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not ctx.args:
        await update.message.reply_text("Напиши вес так: /weight 93")
        return
    try:
        weight = float(ctx.args[0].replace(",", "."))
        log_entry = {"date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"), "weight": weight}
        data.setdefault("weight_log", []).append(log_entry)
        lost = 94 - weight
        left = weight - 80
        save_data(data)
        reply = ask_groq(data, f"Пользователь записал вес: {weight} кг. Сбросил уже {lost:.1f} кг от старта (94 кг), осталось {left:.1f} кг до цели (80 кг). Прокомментируй коротко — прогресс, мотивация.")
        await update.message.reply_text(
            f"⚖️ *{weight} кг записан*\n"
            f"📉 Сброшено: *{lost:.1f} кг*\n"
            f"🎯 До цели: *{left:.1f} кг*\n\n{reply}",
            parse_mode="Markdown"
        )
    except:
        await update.message.reply_text("Не понял. Напиши так: /weight 93")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    w = data.get("weekly_stats", {}).get(get_week_key(), {"done":0,"skipped":0,"total_km":0})
    runs = data.get("runs", [])
    best = max((r.get("distance_km", 0) for r in runs), default=0)
    month_km = data.get("monthly_km", {}).get(get_month_key(), 0)
    weight_log = data.get("weight_log", [])
    weight_str = f"{weight_log[-1]['weight']} кг" if weight_log else "не записан"
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"*Эта неделя:*\n"
        f"✅ Тренировок: {w['done']}  ❌ Пропусков: {w['skipped']}\n"
        f"🏃 Набегано: {w['total_km']:.1f} км\n\n"
        f"*Этот месяц:*\n"
        f"🏃 Всего км: {month_km:.1f} км\n\n"
        f"*Всего:*\n"
        f"🔥 Серия: {data.get('streak',0)} дней\n"
        f"📏 Текущий бег: {data['last_run_km']} км\n"
        f"🏆 Лучшая пробежка: {best} км\n"
        f"📋 Пробежек: {len(runs)}\n"
        f"⚖️ Последний вес: {weight_str}",
        parse_mode="Markdown"
    )

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    psych = data.get("psychology", {})
    ratings = psych.get("avg_rating", [])
    skips = psych.get("skip_patterns", [])
    prefs = data.get("preferences", [])
    lines = ["🧠 *Твой профиль*\n"]
    pref_time = psych.get("preferred_time")
    lines.append(f"⏰ Выходишь: {pref_time if pref_time else 'мало данных'}")
    if ratings:
        avg = sum(ratings[-10:]) / len(ratings[-10:])
        label = ("можно добавлять нагрузку" if avg < 2.5
                 else "следи за восстановлением" if avg > 4
                 else "рабочая зона ✅")
        lines.append(f"💪 Средняя нагрузка: {avg:.1f}/5 — {label}")
    lines.append(f"📉 Пропусков: {len(skips)}")
    if prefs:
        lines.append(f"⚙️ Предпочтения: {', '.join(prefs[-3:])}")
    analysis = ask_groq(data, "Дай психологический анализ пользователя: когда реально выходит, что мотивирует, риски. 3 предложения. Без JSON.")
    lines.append(f"\n💬 *Анализ:*\n{analysis}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── ФОТО ─────────────────────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    msg = await update.message.reply_text("📷 Смотрю...")
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = bytes(await file.download_as_bytearray())
    data = load_data()
    reply = ask_groq(data, "фото", img_bytes)
    result = parse_json(reply)

    # Скрин пробежки
    if result and result.get("type") == "run_result":
        record = {"timestamp": datetime.now(TIMEZONE).isoformat(),
                  **{k:v for k,v in result.items() if k != "type"}}
        data.setdefault("runs", []).append(record)
        km = result.get("distance_km", 0)
        if km:
            add_km(data, km)
            data.setdefault("weekly_stats", {}).setdefault(get_week_key(), {"done":0,"skipped":0,"total_km":0})["done"] += 1
        update_streak(data)
        update_preferred_time(data, result.get("start_time", ""))
        save_data(data)
        await msg.edit_text(
            fmt_run(record) + "\n\n*Как оцениваешь сложность?*",
            parse_mode="Markdown",
            reply_markup=rating_kb()
        )

    # Фото тренажёра
    elif result and result.get("type") == "equipment":
        eq = result
        name = eq.get("name", "Тренажёр")
        muscle = eq.get("muscle_group", "")
        how = eq.get("how_to_use", "")
        sets = eq.get("sets_reps", "4×12")
        note = eq.get("note", "")

        # Сохраняем тренажёр в базу
        equipment_list = data.setdefault("equipment", [])
        names = [e.get("name") for e in equipment_list]
        if name not in names:
            equipment_list.append({"name": name, "muscle_group": muscle, "sets_reps": sets, "note": note})
            save_data(data)
            is_new = True
        else:
            is_new = False

        text = (
            f"💪 *{name}*\n\n"
            f"🎯 Мышцы: {muscle}\n"
            f"📋 Техника: {how}\n"
            f"🔢 Схема: {sets}\n"
        )
        if note:
            text += f"💡 _{note}_\n"

        if is_new:
            text += f"\n✅ Добавил в твой арсенал!"
        else:
            text += f"\n_Этот тренажёр уже в программе_"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Перестрой тренировку под площадку", callback_data="rebuild_workout"),
            InlineKeyboardButton("➕ Ещё тренажёр", callback_data="more_equipment"),
        ]])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

    else:
        await msg.edit_text("Не смог распознать. Это скрин пробежки или фото тренажёра?")

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────
RATE_NEXT = {
    1: "Следующая такая же — главное войти в ритм.",
    2: "Легко прошло — добавим чуть нагрузки.",
    3: "Рабочий режим! Следующая на 10% тяжелее.",
    4: "Огонь! Ты в своей зоне — держим темп.",
    5: "Мощно! Следующая — лёгкое восстановление."
}

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = load_data()

    if q.data.startswith("rate_"):
        n = int(q.data[-1])
        data.setdefault("psychology", {}).setdefault("avg_rating", []).append(n)
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Оценка {n}/5 ✓\n{RATE_NEXT[n]}")

    elif q.data == "accept":
        data["workout_accepted"] = True
        reply = ask_groq(data, "Принял тренировку! Скажи мотивирующее, 1 предложение. Без JSON.")
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🔥 {reply}\n\nКогда закончишь — /done")

    elif q.data == "suggest":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("💬 Расскажи что хочешь изменить — скорректирую!")

    elif q.data == "rebuild_workout":
        data = load_data()
        equipment = data.get("equipment", [])
        eq_list = "\n".join([f"- {e['name']} ({e['muscle_group']})" for e in equipment])
        weekday = datetime.now(TIMEZONE).strftime("%A")
        reply = ask_groq(data,
            f"Перестрой тренировку на сегодня ({weekday}) с учётом доступных тренажёров на площадке:\n{eq_list}\n"
            "Добавь их органично в программу. Ответь ТОЛЬКО чистым JSON workout."
        )
        w = parse_json(reply)
        if w and w.get("type") == "workout":
            data["pending_workout"] = w
            data["workout_accepted"] = False
            save_data(data)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(
                f"🔄 *Тренировка перестроена под твою площадку:*\n\n{fmt_workout(w)}",
                parse_mode="Markdown",
                reply_markup=accept_kb()
            )

    elif q.data == "more_equipment":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("📷 Скидывай следующее фото тренажёра!")
        data["streak"] = 0
        week = get_week_key()
        data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})["skipped"] += 1
        reply = ask_groq(data, "Пропускает сегодня. 1 предложение — пойми и мотивируй на завтра. Без JSON.")
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(reply)

# ─── ЧАТ ──────────────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    data = load_data()
    text = update.message.text

    # Запоминаем предпочтения
    lower = text.lower()
    prefs = data.setdefault("preferences", [])
    for kw in ["не люблю", "терпеть не могу", "обожаю", "люблю", "предпочитаю"]:
        if kw in lower and len(text) < 100:
            if text not in prefs:
                prefs.append(text)

    reply = ask_groq(data, text)
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        data["pending_workout"] = w
        data["workout_accepted"] = False
        save_data(data)
        await update.message.reply_text(fmt_workout(w), parse_mode="Markdown", reply_markup=accept_kb())
    elif w and w.get("type") == "run_result":
        # Вдруг прислал текст с данными — тоже обрабатываем
        await update.message.reply_text("Используй /done чтобы отметить тренировку, или скинь скрин пробежки 📷")
    else:
        await update.message.reply_text(reply)

# ─── ПЛАНИРОВЩИК ──────────────────────────────────────────────────────────────
async def job_wakeup(app: Application):
    """6:30 — подъём с актуальной погодой"""
    data = load_data()
    w = data.get("pending_workout")
    accepted = data.get("workout_accepted", False)

    # Погода на время тренировки (7:00)
    weather = get_weather(target_hour=7)
    weather_str = fmt_weather(weather, "тренировку")

    if w and not accepted:
        text = f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\nВот задание на сегодня:\n\n{fmt_workout(w)}"
        await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown", reply_markup=accept_kb())
    elif w and accepted:
        text = f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\nТы принял вызов — вперёд!\n\n{fmt_workout(w)}"
        await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown")
    else:
        text = f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\nНапиши /workout чтобы получить задание!"
        await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown")

async def job_evening(app: Application):
    """20:00 — задание на завтра с погодой"""
    data = load_data()
    tomorrow = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%A")

    # Проверяем погоду на утро (7:00)
    weather = get_weather(target_hour=7)
    bike_ok = weather.get("bike_ok", True)
    weather_str = fmt_weather(weather, "завтрашнее утро")

    weather_context = ""
    if weather:
        weather_context = (
            f"Погода на завтра утром: {weather.get('summary', '')}. "
            f"{'Велик брать можно — погода хорошая.' if bike_ok else 'Велик НЕ берём — плохая погода, дождь или сильный ветер.'} "
        )

    reply = ask_groq(data,
        f"Составь тренировку на завтра ({tomorrow}). {weather_context}"
        "Учти все данные и психологию. Ответь ТОЛЬКО чистым JSON workout."
    )
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        # Если погода плохая — принудительно убираем велик
        if not bike_ok:
            w["bike"] = False
        data["pending_workout"] = w
        data["workout_accepted"] = False
        save_data(data)
        await app.bot.send_message(
            YOUR_CHAT_ID,
            f"🌙 *Задание на завтра:*{weather_str}\n\n{fmt_workout(w)}",
            parse_mode="Markdown",
            reply_markup=accept_kb()
        )

async def job_motivation(app: Application):
    """14:00 — мотивация если не тренировался"""
    data = load_data()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    if data.get("last_workout_date") != today:
        reply = ask_groq(data, "Не тренировался сегодня. Подстегни — коротко и конкретно, учти его психологию. 1-2 предложения. Без JSON.")
        await app.bot.send_message(YOUR_CHAT_ID, f"⚡ {reply}")

async def job_weekly(app: Application):
    """Воскресенье 19:00 — итоги недели"""
    data = load_data()
    w = data.get("weekly_stats", {}).get(get_week_key(), {"done":0,"skipped":0,"total_km":0})
    prompt = (
        f"Итоги недели: {w['done']} тренировок, {w['skipped']} пропусков, "
        f"{w['total_km']:.1f} км бега, серия {data.get('streak',0)} дней, "
        f"текущий бег {data['last_run_km']} км. "
        "Анализ (4-5 предложений): прогресс, паттерны, цель на след. неделю. Учти психологию. Без JSON."
    )
    reply = ask_groq(data, prompt)
    await app.bot.send_message(
        YOUR_CHAT_ID,
        f"📊 *ИТОГИ НЕДЕЛИ*\n\n"
        f"✅ {w['done']} тренировок  ❌ {w['skipped']} пропусков\n"
        f"🏃 {w['total_km']:.1f} км  🔥 серия {data.get('streak',0)} дней\n\n{reply}",
        parse_mode="Markdown"
    )

async def job_weight_reminder(app: Application):
    """Понедельник 9:00 — напоминание взвеситься"""
    data = load_data()
    weight_log = data.get("weight_log", [])
    last_weight = weight_log[-1]["weight"] if weight_log else 94
    await app.bot.send_message(
        YOUR_CHAT_ID,
        f"⚖️ *Понедельник — день взвешивания!*\n\n"
        f"Последний вес: {last_weight} кг\n"
        f"Запиши сегодняшний: /weight 93.5",
        parse_mode="Markdown"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("workout", cmd_workout))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("skip",    cmd_skip))
    app.add_handler(CommandHandler("weight",  cmd_weight))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_wakeup,          "cron", hour=6,  minute=30, args=[app])
    scheduler.add_job(job_motivation,      "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(job_evening,         "cron", hour=20, minute=0,  args=[app])
    scheduler.add_job(job_weekly,          "cron", day_of_week="sun",  hour=19, args=[app])
    scheduler.add_job(job_weight_reminder, "cron", day_of_week="mon",  hour=9,  args=[app])
    scheduler.start()

    log.info("✅ Бот Макс запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
