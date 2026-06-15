import os, json, logging, base64, urllib.request, urllib.parse, csv, io
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

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
YOUR_CHAT_ID    = int(os.environ["YOUR_CHAT_ID"])
TIMEZONE        = pytz.timezone(os.environ.get("TZ", "Asia/Yekaterinburg"))
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
CITY            = os.environ.get("CITY", "Yekaterinburg")

DATA_FILE = Path("data.json")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)
client = Groq(api_key=GROQ_API_KEY)

TEXT_MODEL   = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-11b-vision-preview"
AUDIO_MODEL  = "whisper-large-v3"

SYSTEM = """Ты — Макс, личный тренер. Говоришь ТОЛЬКО на русском языке. Никаких слов на английском, испанском или любом другом языке — только русский. На "ты", кратко и по делу.

═══ ПРОФИЛЬ ═══
Рост 172 см, вес начальный 94 кг → цель 80 кг (рельеф без объёма, кроссфит-стиль)
Бег: прогрессия от 3 км → цель 10 км регулярно
Площадка: воркаут + уличные тренажёры (турники, брусья, тренажёры с весом)
Велосипед: 2.5 км от дома до площадки
В зал не ходит
Подтягивания тяжело → начинаем с австралийских и негативов. Брусья легко.

═══ ТИПЫ ТРЕНИРОВОК ═══
1. ВЕЛИК + ПЛОЩАДКА: велик туда (2.5 км) → тренировка 40-60 мин → велик домой. Минимум 4 упр × 4 подхода + финишёр 10-15 мин. БЕГ в этот день не добавляем.
2. ТОЛЬКО БЕГ: выходит из дома, бежит. Прогрессия +0.5 км каждые 1-2 недели.
3. АКТИВНЫЙ ОТДЫХ: лёгкая прогулка или велик без нагрузки.

═══ ПРАВИЛА ═══
1. Никогда: велик туда + бег обратно
2. Площадка = полноценная тренировка, не 3 упражнения
3. Финишёр всегда в конце (берпи, AMRAP, табата)
4. Чередуй тяжёлый/лёгкий день
5. Бот не должен надоедать — только по делу
6. Запоминай всё что говорит пользователь о предпочтениях

═══ ФОРМАТЫ JSON ═══
Тренировка: {"type":"workout","title":"...","bike":true,"plan":[{"exercise":"...","sets":4,"reps":"12","note":"..."}],"motivation":"...","estimated_time":"50 мин","difficulty_target":3}
Пробежка: {"type":"run_result","distance_km":5.2,"duration":"28:14","pace_avg":"5:26/км","start_time":"07:15","finish_time":"07:43","calories":420,"app":"Strava","insight":"..."}
Тренажёр: {"type":"equipment","name":"...","muscle_group":"...","how_to_use":"...","sets_reps":"4×12","note":"..."}
Еда: {"type":"food","name":"...","calories":450,"protein":30,"carbs":40,"fat":15,"assessment":"хорошо/норм/плохо","comment":"..."}
Самочувствие: {"type":"feeling","energy":4,"muscles_ok":true,"pain":"нет/колено/спина","comment":"..."}

Для обычного разговора — только текст, без JSON."""

# ─── ДАННЫЕ ───────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {
        "chat_history": [],
        "runs": [],
        "food_log": [],
        "feeling_log": [],
        "weekly_stats": {},
        "monthly_km": {},
        "weight_log": [{"date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"), "weight": 94}],
        "pending_workout": None,
        "workout_accepted": False,
        "last_run_km": 3.0,
        "streak": 0,
        "last_workout_date": None,
        "preferences": [],
        "tomorrow_wishes": [],
        "equipment": [],
        "records": {"max_run_km": 0, "max_pullups": 0, "min_weight": 94},
        "challenges": [],
        "achievements": [],
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
    records = data.get("records", {})
    feeling_log = data.get("feeling_log", [])

    parts = ["[ДАННЫЕ:"]
    parts.append(f"бег {data['last_run_km']} км | серия {data.get('streak',0)} дней")
    if weight_log:
        lw = weight_log[-1]
        parts.append(f"| вес {lw.get('weight',94)} кг (цель 80, осталось {lw.get('weight',94)-80} кг)")
    if runs:
        r = runs[-1]
        parts.append(f"| последний бег: {r.get('distance_km')} км темп {r.get('pace_avg','?')}")
    if w:
        parts.append(f"| неделя: {w.get('done',0)} тр. {w.get('total_km',0):.1f} км")
    if monthly:
        parts.append(f"| месяц: {monthly:.1f} км")
    if records.get("max_run_km"):
        parts.append(f"| рекорд бега: {records['max_run_km']} км")
    if feeling_log:
        lf = feeling_log[-1]
        parts.append(f"| самочувствие: энергия {lf.get('energy',3)}/5 боль {lf.get('pain','нет')}")
    if psych.get("avg_rating"):
        avg = sum(psych["avg_rating"][-5:]) / len(psych["avg_rating"][-5:])
        parts.append(f"| средняя оценка: {avg:.1f}/5")
    if prefs:
        parts.append(f"| предпочтения: {'; '.join(prefs[-3:])}")
    parts.append("]")
    return " ".join(parts)

# ─── ПОГОДА ───────────────────────────────────────────────────────────────────
def get_weather(target_hour: int = 7) -> dict:
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
        best, best_diff = None, 999
        for item in data.get("list", []):
            dt_txt = item.get("dt_txt", "")
            if not dt_txt.startswith(tomorrow):
                continue
            diff = abs(int(dt_txt[11:13]) - target_hour)
            if diff < best_diff:
                best_diff, best = diff, item
        if not best:
            return {}
        main = best["main"]
        wind = best.get("wind", {})
        rain = best.get("rain", {}).get("3h", 0)
        snow = best.get("snow", {}).get("3h", 0)
        desc = best["weather"][0]["description"] if best.get("weather") else ""
        temp = round(main.get("temp", 0))
        feels = round(main.get("feels_like", 0))
        speed = round(wind.get("speed", 0))
        is_bad = rain > 0.5 or snow > 0 or speed > 10
        emoji = ("🌧" if rain > 0.5 else "❄️" if snow > 0 else "💨" if speed > 10
                 else "⛅" if "облач" in desc else "☀️")
        return {
            "temp": temp, "feels": feels, "desc": desc,
            "wind_speed": speed, "rain": rain, "snow": snow,
            "is_bad": is_bad, "emoji": emoji, "bike_ok": not is_bad,
            "summary": f"{emoji} {temp}°C, {desc}, ветер {speed} м/с" + (f", дождь {rain} мм" if rain else ""),
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
    lines.append("   _↳ " + ("Велик не берём 🚫" if w.get("is_bad") else "Велик берём! 🚴_"))
    return "\n".join(lines)

# ─── GROQ ─────────────────────────────────────────────────────────────────────
def ask_groq(data: dict, user_message: str, image_bytes: bytes = None, audio_bytes: bytes = None) -> str:
    ctx = build_context(data)
    try:
        if audio_bytes:
            transcription = client.audio.transcriptions.create(
                file=("voice.ogg", audio_bytes, "audio/ogg"),
                model=AUDIO_MODEL,
                language="ru"
            )
            user_message = transcription.text
            log.info(f"Voice transcribed: {user_message}")

        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": f"{ctx}\nПосмотри на фото. Если скрин пробежки — JSON run_result. Если тренажёр/снаряд — JSON equipment. Если еда — JSON food. Ответь ТОЛЬКО чистым JSON."},
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
        reply = reply.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        history = data.get("chat_history", [])
        history.append({"role": "user", "content": user_message or "📷"})
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

# ─── ФОРМАТИРОВАНИЕ ───────────────────────────────────────────────────────────
def fmt_workout(w: dict) -> str:
    em = "🚴" if w.get("bike") else "🏃"
    lines = [f"{em} *{w['title']}*", f"⏱ {w.get('estimated_time','?')}"]
    if w.get("bike"):
        lines.append("_🚲 Велик до площадки → тренировка → велик домой_\n")
    else:
        lines.append("")
    for i, ex in enumerate(w.get("plan", []), 1):
        s, r = ex.get("sets", 1), ex.get("reps", "")
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

def fmt_food(f: dict) -> str:
    ass_emoji = {"хорошо": "✅", "норм": "👍", "плохо": "⚠️"}.get(f.get("assessment","норм"), "👍")
    lines = [f"{ass_emoji} *{f.get('name','Приём пищи')}*\n"]
    if f.get("calories"): lines.append(f"🔥 Калории: {f['calories']} ккал")
    if f.get("protein"):  lines.append(f"💪 Белок: {f['protein']} г")
    if f.get("carbs"):    lines.append(f"⚡ Углеводы: {f['carbs']} г")
    if f.get("fat"):      lines.append(f"🥑 Жиры: {f['fat']} г")
    if f.get("comment"):  lines.append(f"\n💬 _{f['comment']}_")
    return "\n".join(lines)

def accept_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принимаю!", callback_data="accept"),
        InlineKeyboardButton("💬 Изменить",  callback_data="suggest"),
        InlineKeyboardButton("😴 Пропустить", callback_data="skip_today"),
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
    week, month = get_week_key(), get_month_key()
    data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    data["weekly_stats"][week]["total_km"] += km
    data.setdefault("monthly_km", {})[month] = data["monthly_km"].get(month, 0) + km
    data["last_run_km"] = km
    # Обновляем рекорд
    if km > data.get("records", {}).get("max_run_km", 0):
        data.setdefault("records", {})["max_run_km"] = km
        return True  # новый рекорд!
    return False

def update_preferred_time(data: dict, start_time: str):
    if not start_time:
        return
    try:
        h = int(start_time.split(":")[0])
        label = ("раннее утро" if h < 9 else "утро (9–12)" if h < 12
                 else "день" if h < 17 else "вечер")
        times = data.setdefault("psychology", {}).setdefault("all_start_times", [])
        times.append(label)
        data["psychology"]["preferred_time"] = max(set(times), key=times.count)
    except:
        pass

def check_achievements(data: dict) -> list[str]:
    """Проверяет новые достижения и возвращает список новых."""
    new = []
    achievements = data.setdefault("achievements", [])
    runs = data.get("runs", [])
    records = data.get("records", {})
    streak = data.get("streak", 0)
    total_runs = len(runs)

    checks = [
        ("first_run",    "🏅 Первая пробежка зафиксирована!",        total_runs >= 1),
        ("run_5km",      "🏅 Первые 5 км без остановки!",             records.get("max_run_km", 0) >= 5),
        ("run_7km",      "🏅 7 км — ты уже серьёзный бегун!",         records.get("max_run_km", 0) >= 7),
        ("run_10km",     "🏅 10 КМ! ЦЕЛЬ ДОСТИГНУТА! 🎉",             records.get("max_run_km", 0) >= 10),
        ("streak_7",     "🔥 7 дней подряд без пропусков!",           streak >= 7),
        ("streak_30",    "🔥 30 дней подряд — железная воля!",        streak >= 30),
        ("runs_10",      "📋 10 пробежек зафиксировано!",             total_runs >= 10),
        ("runs_50",      "📋 50 пробежек — ты машина!",               total_runs >= 50),
        ("weight_90",    "⚖️ Вес ниже 90 кг — первый рубеж!",         data.get("weight_log", [{}])[-1].get("weight", 94) < 90),
        ("weight_85",    "⚖️ 85 кг — уже заметно!",                   data.get("weight_log", [{}])[-1].get("weight", 94) < 85),
        ("weight_80",    "⚖️ 80 КГ! ЦЕЛЬ ПО ВЕСУ ДОСТИГНУТА! 🎉",     data.get("weight_log", [{}])[-1].get("weight", 94) <= 80),
    ]
    for key, msg, condition in checks:
        if condition and key not in achievements:
            achievements.append(key)
            new.append(msg)
    return new

def check_plateau(data: dict) -> bool:
    """Проверяет стагнацию веса (2+ недели без изменений)."""
    weight_log = data.get("weight_log", [])
    if len(weight_log) < 4:
        return False
    recent = [w["weight"] for w in weight_log[-4:]]
    return max(recent) - min(recent) < 0.5

def check_challenge_progress(data: dict) -> str | None:
    """Проверяет активные челленджи."""
    challenges = data.get("challenges", [])
    for ch in challenges:
        if ch.get("done"):
            continue
        if ch["type"] == "no_skip" and data.get("streak", 0) >= ch["target"]:
            ch["done"] = True
            return f"🏆 Челлендж выполнен: {ch['name']}!"
        elif ch["type"] == "run_km":
            total = data.get("monthly_km", {}).get(get_month_key(), 0)
            if total >= ch["target"]:
                ch["done"] = True
                return f"🏆 Челлендж выполнен: {ch['name']}!"
    return None

# ─── ЭКСПОРТ ──────────────────────────────────────────────────────────────────
def export_csv(data: dict) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Дата", "Тип", "Км", "Темп", "Время", "Вес", "Оценка"])
    runs = data.get("runs", [])
    weight_log = {w["date"]: w["weight"] for w in data.get("weight_log", [])}
    ratings = data.get("psychology", {}).get("avg_rating", [])
    for i, r in enumerate(runs):
        date = r.get("timestamp", "")[:10]
        writer.writerow([
            date, "Бег",
            r.get("distance_km", ""),
            r.get("pace_avg", ""),
            r.get("duration", ""),
            weight_log.get(date, ""),
            ratings[i] if i < len(ratings) else ""
        ])
    return output.getvalue().encode("utf-8-sig")

# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💪 *Привет! Я Макс — твой личный тренер.*\n\n"
        "📷 Скинь скрин пробежки — всё зафиксирую\n"
        "🍽 Скинь фото еды — оценю калории и белки\n"
        "🎤 Говори голосом — понимаю русский\n"
        "🌙 *20:00* — план на завтра\n"
        "⏰ *6:30* — подъём с кнопками принятия\n"
        "📊 *Воскресенье* — итоги и сравнение недель\n"
        "⚖️ *Понедельник* — спрошу вес\n\n"
        "/workout — тренировка сейчас\n"
        "/done — выполнил\n"
        "/skip причина — пропустить\n"
        "/weight 93 — записать вес\n"
        "/stats — статистика\n"
        "/records — личные рекорды\n"
        "/challenge — активный челлендж\n"
        "/feeling — как себя чувствую\n"
        "/export — скачать все данные\n"
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

    # Учитываем самочувствие
    feeling_log = data.get("feeling_log", [])
    feeling_ctx = ""
    if feeling_log:
        lf = feeling_log[-1]
        if lf.get("pain") and lf["pain"] != "нет":
            feeling_ctx = f"Пользователь сообщал о боли: {lf['pain']}. Избегай нагрузку на эту зону. "

    msg = await update.message.reply_text("⏳ Составляю тренировку...")
    reply = ask_groq(data,
        f"Составь тренировку на сегодня ({weekday}). Погода: {weather_context}. "
        f"{'Велик можно.' if bike_ok else 'Велик НЕ берём.'} {feeling_ctx}"
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

    # Проверяем достижения
    new_achievements = check_achievements(data)
    challenge_done = check_challenge_progress(data)
    save_data(data)

    reply = ask_groq(data, f"Тренировка выполнена! {comment} Похвали коротко (1-2 предл.) и попроси оценку 1-5.")
    text = f"🎉 *Засчитано! Серия: {data['streak']} дней* 🔥\n\n{reply}"
    if new_achievements:
        text += "\n\n" + "\n".join(new_achievements)
    if challenge_done:
        text += f"\n\n{challenge_done}"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=rating_kb())

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    week = get_week_key()
    data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})
    data["weekly_stats"][week]["skipped"] += 1
    data["streak"] = 0
    reason = " ".join(ctx.args) if ctx.args else "без причины"
    data.setdefault("psychology", {}).setdefault("skip_patterns", []).append(
        {"date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"), "reason": reason}
    )
    reply = ask_groq(data, f"Пропустил. Причина: {reason}. Пойми, не ругай, 1-2 предл. Без JSON.")
    save_data(data)
    await update.message.reply_text(reply)

async def cmd_weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not ctx.args:
        await update.message.reply_text("Напиши так: /weight 93")
        return
    try:
        weight = float(ctx.args[0].replace(",", "."))
        data.setdefault("weight_log", []).append({
            "date": datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
            "weight": weight
        })
        lost = 94 - weight
        left = weight - 80
        new_ach = check_achievements(data)
        is_plateau = check_plateau(data)
        save_data(data)

        plateau_ctx = "Вес стоит уже 2+ недели — предложи что изменить (питание/интенсивность). " if is_plateau else ""
        reply = ask_groq(data,
            f"Вес: {weight} кг. Сброшено {lost:.1f} кг, осталось {left:.1f} кг. {plateau_ctx}"
            "Прокомментируй коротко. Без JSON."
        )
        text = (
            f"⚖️ *{weight} кг*\n"
            f"📉 Сброшено: *{lost:.1f} кг*\n"
            f"🎯 До цели: *{left:.1f} кг*\n\n{reply}"
        )
        if new_ach:
            text += "\n\n" + "\n".join(new_ach)
        await update.message.reply_text(text, parse_mode="Markdown")
    except:
        await update.message.reply_text("Не понял. Напиши так: /weight 93")

async def cmd_feeling(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    reply = ask_groq(data,
        "Спроси как пользователь себя чувствует — энергия, болят ли мышцы, есть ли боли. "
        "Коротко, 1-2 вопроса. Без JSON."
    )
    await update.message.reply_text(reply)

async def cmd_records(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    r = data.get("records", {})
    runs = data.get("runs", [])
    best_pace = ""
    if runs:
        paces = [(r.get("pace_avg",""), r.get("distance_km",0)) for r in runs if r.get("pace_avg")]
        if paces:
            best_pace = min(paces, key=lambda x: x[0])[0]
    await update.message.reply_text(
        f"🏆 *Личные рекорды*\n\n"
        f"🏃 Макс дистанция: *{r.get('max_run_km', 0)} км*\n"
        f"⚡ Лучший темп: *{best_pace or 'нет данных'}*\n"
        f"💪 Подтягивания: *{r.get('max_pullups', 0)} повт.*\n"
        f"📋 Всего пробежек: *{len(runs)}*\n"
        f"🔥 Макс серия: *{r.get('max_streak', data.get('streak',0))} дней*",
        parse_mode="Markdown"
    )

async def cmd_challenge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    challenges = data.get("challenges", [])
    active = [c for c in challenges if not c.get("done")]
    if active:
        ch = active[0]
        if ch["type"] == "no_skip":
            current = data.get("streak", 0)
            text = (
                f"🎯 *Активный челлендж:* {ch['name']}\n\n"
                f"Прогресс: {current}/{ch['target']} дней\n"
                f"{'▓' * current}{'░' * (ch['target']-current)}"
            )
        else:
            current = data.get("monthly_km", {}).get(get_month_key(), 0)
            text = (
                f"🎯 *Активный челлендж:* {ch['name']}\n\n"
                f"Прогресс: {current:.1f}/{ch['target']} км"
            )
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        # Предлагаем челлендж
        streak = data.get("streak", 0)
        if streak >= 3:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("7 дней подряд", callback_data="ch_7days"),
                InlineKeyboardButton("30 дней подряд", callback_data="ch_30days"),
            ],[
                InlineKeyboardButton("50 км за месяц", callback_data="ch_50km"),
                InlineKeyboardButton("100 км за месяц", callback_data="ch_100km"),
            ]])
            await update.message.reply_text(
                "🎯 *Выбери челлендж:*",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(
                "Потренируйся ещё немного — и предложу тебе первый челлендж! 💪"
            )

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    csv_bytes = export_csv(data)
    month = datetime.now(TIMEZONE).strftime("%Y-%m")
    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=f"тренировки_{month}.csv",
        caption=f"📊 Все твои данные за {month}"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    w = data.get("weekly_stats", {}).get(get_week_key(), {"done":0,"skipped":0,"total_km":0})
    runs = data.get("runs", [])
    best = max((r.get("distance_km", 0) for r in runs), default=0)
    month_km = data.get("monthly_km", {}).get(get_month_key(), 0)
    weight_log = data.get("weight_log", [])
    weight_str = f"{weight_log[-1]['weight']} кг" if weight_log else "не записан"
    challenges = [c for c in data.get("challenges", []) if not c.get("done")]
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"*Эта неделя:*\n"
        f"✅ Тренировок: {w['done']}  ❌ Пропусков: {w['skipped']}\n"
        f"🏃 Набегано: {w['total_km']:.1f} км\n\n"
        f"*Этот месяц:* {month_km:.1f} км\n\n"
        f"*Всего:*\n"
        f"🔥 Серия: {data.get('streak',0)} дней\n"
        f"📏 Текущий бег: {data['last_run_km']} км\n"
        f"🏆 Рекорд: {best} км\n"
        f"📋 Пробежек: {len(runs)}\n"
        f"⚖️ Вес: {weight_str}\n"
        + (f"🎯 Челленджей: {len(challenges)} активных" if challenges else ""),
        parse_mode="Markdown"
    )

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    psych = data.get("psychology", {})
    ratings = psych.get("avg_rating", [])
    skips = psych.get("skip_patterns", [])
    lines = ["🧠 *Психопрофиль*\n"]
    lines.append(f"⏰ Выходишь: {psych.get('preferred_time') or 'мало данных'}")
    if ratings:
        avg = sum(ratings[-10:]) / len(ratings[-10:])
        label = ("можно добавлять" if avg < 2.5 else "на пределе" if avg > 4 else "рабочая зона ✅")
        lines.append(f"💪 Нагрузка: {avg:.1f}/5 — {label}")
    lines.append(f"📉 Пропусков: {len(skips)}")
    lines.append(f"🏅 Достижений: {len(data.get('achievements', []))}")
    analysis = ask_groq(data, "Психологический анализ: когда выходит, что мотивирует, риски. 3 предл. Без JSON.")
    lines.append(f"\n💬 *Анализ:*\n{analysis}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── ФОТО ─────────────────────────────────────────────────────────────────────
async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    caption = (update.message.caption or "").lower()
    msg = await update.message.reply_text("📷 Смотрю...")
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = bytes(await file.download_as_bytearray())
    data = load_data()
    reply = ask_groq(data, "фото", img_bytes)
    result = parse_json(reply)

    if result and result.get("type") == "run_result":
        record = {"timestamp": datetime.now(TIMEZONE).isoformat(),
                  **{k:v for k,v in result.items() if k != "type"}}
        data.setdefault("runs", []).append(record)
        km = result.get("distance_km", 0)
        is_record = False
        if km:
            is_record = add_km(data, km)
            data.setdefault("weekly_stats", {}).setdefault(get_week_key(), {"done":0,"skipped":0,"total_km":0})["done"] += 1
        update_streak(data)
        update_preferred_time(data, result.get("start_time", ""))
        new_ach = check_achievements(data)
        save_data(data)
        text = fmt_run(record)
        if is_record:
            text += f"\n\n🏆 *Новый рекорд дистанции — {km} км!*"
        if new_ach:
            text += "\n\n" + "\n".join(new_ach)
        text += "\n\n*Как оцениваешь сложность?*"
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=rating_kb())

    elif result and result.get("type") == "equipment":
        eq = result
        name = eq.get("name", "Тренажёр")
        equipment_list = data.setdefault("equipment", [])
        is_new = name not in [e.get("name") for e in equipment_list]
        if is_new:
            equipment_list.append({"name": name, "muscle_group": eq.get("muscle_group",""),
                                   "sets_reps": eq.get("sets_reps","4×12"), "note": eq.get("note","")})
            save_data(data)
        text = (
            f"💪 *{name}*\n\n"
            f"🎯 {eq.get('muscle_group','')}\n"
            f"📋 {eq.get('how_to_use','')}\n"
            f"🔢 {eq.get('sets_reps','')}\n"
        )
        if eq.get("note"):
            text += f"💡 _{eq['note']}_\n"
        text += f"\n{'✅ Добавил в арсенал!' if is_new else '_Уже в программе_'}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Перестроить тренировку", callback_data="rebuild_workout"),
            InlineKeyboardButton("➕ Ещё тренажёр", callback_data="more_equipment"),
        ]])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif result and result.get("type") == "food":
        record = {"timestamp": datetime.now(TIMEZONE).isoformat(),
                  **{k:v for k,v in result.items() if k != "type"}}
        data.setdefault("food_log", []).append(record)
        # Считаем калории за день
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        today_cal = sum(
            f.get("calories", 0) for f in data["food_log"]
            if f.get("timestamp", "")[:10] == today
        )
        save_data(data)
        text = fmt_food(result) + f"\n\n📊 _Калорий сегодня: ~{today_cal} ккал_"
        await msg.edit_text(text, parse_mode="Markdown")
    else:
        await msg.edit_text("Не смог распознать. Это скрин пробежки, фото тренажёра или еды?")

# ─── ГОЛОС ────────────────────────────────────────────────────────────────────
async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    msg = await update.message.reply_text("🎤 Слушаю...")
    voice = update.message.voice
    file = await ctx.bot.get_file(voice.file_id)
    audio_bytes = bytes(await file.download_as_bytearray())
    data = load_data()
    reply = ask_groq(data, "", audio_bytes=audio_bytes)
    await msg.edit_text(f"🎤 _{reply[:100]}..._\n\n" if len(reply) > 100 else f"🎤 _{reply}_\n\n", parse_mode="Markdown")
    # Отвечаем как на обычное сообщение
    response = ask_groq(data, reply)
    w = parse_json(response)
    if w and w.get("type") == "workout":
        data["pending_workout"] = w
        save_data(data)
        await update.message.reply_text(fmt_workout(w), parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await update.message.reply_text(response)

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────
RATE_NEXT = {
    1: "Главное — вышел. Следующая такая же.",
    2: "Легко прошло — добавим чуть нагрузки.",
    3: "Рабочий режим! Следующая на 10% тяжелее.",
    4: "Огонь! Ты в своей зоне.",
    5: "Мощно! Следующая — лёгкое восстановление."
}

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = load_data()

    if q.data.startswith("rate_"):
        n = int(q.data[-1])
        data.setdefault("psychology", {}).setdefault("avg_rating", []).append(n)
        # Обновляем рекорд серии
        streak = data.get("streak", 0)
        if streak > data.get("records", {}).get("max_streak", 0):
            data.setdefault("records", {})["max_streak"] = streak
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Оценка {n}/5 ✓\n{RATE_NEXT[n]}")

    elif q.data == "accept":
        data["workout_accepted"] = True
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(
            "🔥 *Принято! Вперёд!*\n\n"
            "Когда закончишь — /done и скрин тренировки 📷",
            parse_mode="Markdown"
        )

    elif q.data == "suggest":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("💬 Расскажи что хочешь изменить — скорректирую!")

    elif q.data == "skip_today":
        data["streak"] = 0
        week = get_week_key()
        data.setdefault("weekly_stats", {}).setdefault(week, {"done":0,"skipped":0,"total_km":0})["skipped"] += 1
        reply = ask_groq(data, "Пропускает сегодня. 1 предл., пойми и мотивируй на завтра. Без JSON.")
        save_data(data)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(reply)

    elif q.data == "rebuild_workout":
        equipment = data.get("equipment", [])
        eq_list = "\n".join([f"- {e['name']} ({e['muscle_group']})" for e in equipment])
        weekday = datetime.now(TIMEZONE).strftime("%A")
        reply = ask_groq(data,
            f"Перестрой тренировку на {weekday} с тренажёрами:\n{eq_list}\n"
            "Ответь ТОЛЬКО чистым JSON workout."
        )
        w = parse_json(reply)
        if w and w.get("type") == "workout":
            data["pending_workout"] = w
            data["workout_accepted"] = False
            save_data(data)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(
                f"🔄 *Перестроено под твою площадку:*\n\n{fmt_workout(w)}",
                parse_mode="Markdown", reply_markup=accept_kb()
            )

    elif q.data == "more_equipment":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("📷 Скидывай следующее фото тренажёра!")

    elif q.data.startswith("ch_"):
        challenges_map = {
            "ch_7days":  {"name": "7 дней подряд", "type": "no_skip", "target": 7},
            "ch_30days": {"name": "30 дней подряд", "type": "no_skip", "target": 30},
            "ch_50km":   {"name": "50 км за месяц", "type": "run_km", "target": 50},
            "ch_100km":  {"name": "100 км за месяц", "type": "run_km", "target": 100},
        }
        ch = challenges_map.get(q.data)
        if ch:
            data.setdefault("challenges", []).append({**ch, "done": False,
                "started": datetime.now(TIMEZONE).strftime("%Y-%m-%d")})
            save_data(data)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(
                f"🎯 *Челлендж принят: {ch['name']}!*\n\nПроверить прогресс: /challenge",
                parse_mode="Markdown"
            )

# ─── ЧАТ ──────────────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != YOUR_CHAT_ID:
        return
    data = load_data()
    text = update.message.text
    lower = text.lower()

    # Запоминаем предпочтения
    prefs = data.setdefault("preferences", [])
    for kw in ["не люблю", "терпеть не могу", "обожаю", "люблю", "предпочитаю"]:
        if kw in lower and len(text) < 100:
            if text not in prefs:
                prefs.append(text)

    # Пожелания на завтра — запоминаем, не присылаем тренировку
    tomorrow_hints = ["завтра", "хочу побегать", "хочу бег", "хочу на площадку",
                      "хочу велик", "планирую", "собираюсь"]
    if any(h in lower for h in tomorrow_hints):
        data.setdefault("tomorrow_wishes", []).append(text)
        save_data(data)
        reply = ask_groq(data,
            f"Пользователь говорит о планах: '{text}'. "
            "Ответь тепло, запомни пожелание, скажи что учтёшь в плане в 20:00. "
            "НЕ присылай тренировку. Без JSON."
        )
        await update.message.reply_text(reply)
        return

    # Самочувствие в тексте — сохраняем
    pain_hints = ["болит", "боль", "устал", "ломит", "тянет", "колено", "спина", "плечо"]
    if any(h in lower for h in pain_hints):
        reply = ask_groq(data,
            f"Пользователь говорит о самочувствии: '{text}'. "
            "Уточни где болит и как сильно. Запомни чтобы учесть в тренировке. Без JSON."
        )
        # Сохраняем в feeling_log
        data.setdefault("feeling_log", []).append({
            "timestamp": datetime.now(TIMEZONE).isoformat(),
            "pain": text[:50],
            "energy": 3
        })
        save_data(data)
        await update.message.reply_text(reply)
        return

    # Обычный разговор
    reply = ask_groq(data,
        f"Пользователь: '{text}'. Ответь как тренер — коротко, по делу. "
        "НЕ присылай тренировку если не просит явно. Без JSON."
    )
    save_data(data)
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        await update.message.reply_text(
            "Вот примерный план — финальное задание пришлю в 20:00:\n\n" + fmt_workout(w),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(reply)

# ─── ПЛАНИРОВЩИК ──────────────────────────────────────────────────────────────
async def job_wakeup(app: Application):
    """6:30 — подъём с кнопками принятия"""
    data = load_data()
    w = data.get("pending_workout")
    weather = get_weather(target_hour=7)
    weather_str = fmt_weather(weather, "сегодня")

    if data.get("workout_accepted"):
        text = (f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\n"
                f"Ты принял тренировку — вперёд!\n\n"
                f"{fmt_workout(w) if w else ''}\n\n"
                f"Когда закончишь — /done и скрин 📷")
        await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown")
    elif w:
        text = f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\nЗадание на сегодня:\n\n{fmt_workout(w)}"
        await app.bot.send_message(YOUR_CHAT_ID, text, parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await app.bot.send_message(YOUR_CHAT_ID,
            f"🌅 *6:30 — Подъём!* 💪{weather_str}\n\nНапиши /workout для задания!",
            parse_mode="Markdown")

async def job_evening(app: Application):
    """20:00 — план на завтра (информационно)"""
    data = load_data()
    tomorrow = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%A")
    weather = get_weather(target_hour=7)
    bike_ok = weather.get("bike_ok", True)
    weather_str = fmt_weather(weather, "завтрашнее утро")
    weather_ctx = f"Погода: {weather.get('summary','')}. {'Велик берём.' if bike_ok else 'Велик НЕ берём.'} " if weather else ""
    wishes = data.get("tomorrow_wishes", [])
    wishes_ctx = f"Пожелания: {'; '.join(wishes[-3:])}. Учти! " if wishes else ""
    if wishes:
        data["tomorrow_wishes"] = []

    # Учитываем самочувствие
    feeling_log = data.get("feeling_log", [])
    feeling_ctx = ""
    if feeling_log:
        lf = feeling_log[-1]
        if lf.get("pain") and "нет" not in str(lf.get("pain","")):
            feeling_ctx = f"Боль: {lf['pain']} — избегай эту зону. "

    reply = ask_groq(data,
        f"Составь план на завтра ({tomorrow}). {weather_ctx}{wishes_ctx}{feeling_ctx}"
        "Ответь ТОЛЬКО чистым JSON workout."
    )
    w = parse_json(reply)
    if w and w.get("type") == "workout":
        if not bike_ok:
            w["bike"] = False
        data["pending_workout"] = w
        data["workout_accepted"] = False
        save_data(data)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Хочу изменить", callback_data="suggest"),
        ]])
        await app.bot.send_message(
            YOUR_CHAT_ID,
            f"🌙 *План на завтра:*{weather_str}\n\n{fmt_workout(w)}\n\n"
            f"_Утром в 6:30 — напоминание с кнопками принятия._",
            parse_mode="Markdown", reply_markup=keyboard
        )

async def job_motivation(app: Application):
    """14:00 — мотивация если не тренировался"""
    data = load_data()
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    if data.get("last_workout_date") != today:
        reply = ask_groq(data,
            "Не тренировался сегодня. Подстегни — коротко, учти психологию. 1-2 предл. Без JSON.")
        await app.bot.send_message(YOUR_CHAT_ID, f"⚡ {reply}")

async def job_weekly(app: Application):
    """Воскресенье 19:00 — итоги с сравнением недель"""
    data = load_data()
    stats = data.get("weekly_stats", {})
    week_keys = sorted(stats.keys())

    this_week = stats.get(get_week_key(), {"done":0,"skipped":0,"total_km":0})

    # Прошлая неделя для сравнения
    prev_key = None
    if len(week_keys) >= 2:
        prev_key = week_keys[-2]
    prev_week = stats.get(prev_key, {}) if prev_key else {}

    comparison = ""
    if prev_week:
        diff_km = this_week.get("total_km",0) - prev_week.get("total_km",0)
        diff_done = this_week.get("done",0) - prev_week.get("done",0)
        comparison = (
            f"\n*Сравнение с прошлой неделей:*\n"
            f"{'📈' if diff_km >= 0 else '📉'} Км: {'+' if diff_km>=0 else ''}{diff_km:.1f}\n"
            f"{'📈' if diff_done >= 0 else '📉'} Тренировок: {'+' if diff_done>=0 else ''}{diff_done}\n"
        )

    prompt = (
        f"Итоги: {this_week.get('done',0)} тр., {this_week.get('skipped',0)} пропусков, "
        f"{this_week.get('total_km',0):.1f} км, серия {data.get('streak',0)} дней. "
        + (f"Прошлая неделя: {prev_week.get('total_km',0):.1f} км, {prev_week.get('done',0)} тр. " if prev_week else "")
        + "Анализ (3-4 предл.): прогресс, паттерны, цель на след. неделю. Без JSON."
    )
    reply = ask_groq(data, prompt)
    await app.bot.send_message(
        YOUR_CHAT_ID,
        f"📊 *ИТОГИ НЕДЕЛИ*\n\n"
        f"✅ {this_week.get('done',0)} тр.  ❌ {this_week.get('skipped',0)} пропусков\n"
        f"🏃 {this_week.get('total_km',0):.1f} км  🔥 серия {data.get('streak',0)} дней\n"
        f"{comparison}\n{reply}",
        parse_mode="Markdown"
    )

async def job_weight_reminder(app: Application):
    """Понедельник 9:00 — взвешивание"""
    data = load_data()
    weight_log = data.get("weight_log", [])
    last = weight_log[-1]["weight"] if weight_log else 94
    await app.bot.send_message(
        YOUR_CHAT_ID,
        f"⚖️ *Взвешивание!*\nПоследний вес: {last} кг\n\nЗапиши: /weight 93.5",
        parse_mode="Markdown"
    )

async def job_monthly_export(app: Application):
    """1-е число месяца — экспорт данных"""
    data = load_data()
    prev_month = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m")
    csv_bytes = export_csv(data)
    await app.bot.send_document(
        YOUR_CHAT_ID,
        document=io.BytesIO(csv_bytes),
        filename=f"тренировки_{prev_month}.csv",
        caption=f"📊 Твои данные за {prev_month} — держи архив!"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("workout",   cmd_workout))
    app.add_handler(CommandHandler("done",      cmd_done))
    app.add_handler(CommandHandler("skip",      cmd_skip))
    app.add_handler(CommandHandler("weight",    cmd_weight))
    app.add_handler(CommandHandler("feeling",   cmd_feeling))
    app.add_handler(CommandHandler("records",   cmd_records))
    app.add_handler(CommandHandler("challenge", cmd_challenge))
    app.add_handler(CommandHandler("export",    cmd_export))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("profile",   cmd_profile))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(job_wakeup,         "cron", hour=6,  minute=30, args=[app])
    scheduler.add_job(job_motivation,     "cron", hour=14, minute=0,  args=[app])
    scheduler.add_job(job_evening,        "cron", hour=20, minute=0,  args=[app])
    scheduler.add_job(job_weekly,         "cron", day_of_week="sun",  hour=19, args=[app])
    scheduler.add_job(job_weight_reminder,"cron", day_of_week="mon",  hour=9,  args=[app])
    scheduler.add_job(job_monthly_export, "cron", day=1, hour=10, args=[app])
    scheduler.start()

    log.info("✅ Бот Макс запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
