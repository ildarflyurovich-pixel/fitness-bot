"""Тренер Макс v4.0 — надёжный, умный, без компромиссов"""
import os, json, logging, base64, urllib.request, urllib.parse, csv, io, threading, re
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from groq import Groq
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
YOUR_CHAT_ID    = int(os.environ["YOUR_CHAT_ID"])
TIMEZONE        = pytz.timezone(os.environ.get("TZ", "Asia/Yekaterinburg"))
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
CITY            = os.environ.get("CITY", "Yekaterinburg")
PORT            = int(os.environ.get("PORT", 8080))

DATA_FILE = Path("data.json")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
log = logging.getLogger(__name__)
groq = Groq(api_key=GROQ_API_KEY)

TEXT_MODEL   = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-90b-vision-preview"
AUDIO_MODEL  = "whisper-large-v3"

# ─── KEEP ALIVE (Railway не убьёт процесс) ────────────────────────────────────
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Max Trainer OK")
    def log_message(self, *a): pass

threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()

# ─── СИСТЕМНЫЙ ПРОМПТ ─────────────────────────────────────────────────────────
SYSTEM = """Ты — Макс, личный тренер. ТОЛЬКО русский язык. На "ты". Кратко, по делу, без воды.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
Рост 172, вес 94 кг → цель 80 кг. Рельеф без объёма. Кроссфит-стиль.
Бег: сейчас 3-5 км → цель 10 км. +0.5 км каждые 1-2 недели.
Площадка FOREMAN: турники, брусья, тяга горизонтальная, гиперэкстензия, жим ногами, наклонная скамья для пресса, наклонная скамья для жима, шведская стенка, горизонтальная лестница.
Велосипед: 2.5 км до площадки (разминка) и 2.5 км домой (заминка).
В зал не ходит.

3 ТИПА ТРЕНИРОВОК:
1. ВЕЛИК+ПЛОЩАДКА: велик туда → 40-60 мин (5+ упр × 4-5 подходов + финишёр 10-15 мин) → велик домой. БЕГ в этот день не нужен.
2. ТОЛЬКО БЕГ: выходит и бежит. Прогрессия дистанции. Без велика и площадки.
3. БЕГ+ПЛОЩАДКА: сначала бег (сокращённая дистанция -1 км от обычной), потом площадка (укороченная, 30-40 мин). Велик не нужен.
4. ОТДЫХ: лёгкая прогулка или полный отдых.

РАСПИСАНИЕ НА НЕДЕЛЮ (рекомендация, пользователь может менять):
Пн: Велик+Площадка
Вт: Бег
Ср: Отдых
Чт: Велик+Площадка
Пт: Бег
Сб: Длинный бег
Вс: Отдых

ЕСЛИ ПОЛЬЗОВАТЕЛЬ ПРОСИТ ДРУГОЕ — всегда слушай его. Расписание вторично.
ЗАПРЕЩЕНО: велик туда + бег обратно. Тренировка из 3 упражнений по 3 подхода.
Финишёр обязателен на площадке: берпи / AMRAP / табата / спринты.
Бег прогрессирует: +0.5 км каждые 1-2 недели к цели 10 км.

АДАПТАЦИЯ ПО ОЦЕНКАМ: 1-2 → легче или так же. 3 → +10% нагрузки. 4-5 → держи темп или восстановление.

JSON ФОРМАТЫ (без markdown):
workout: {"type":"workout","title":"...","bike":true,"plan":[{"exercise":"...","sets":4,"reps":"12","note":"..."}],"motivation":"...","estimated_time":"50 мин","difficulty_target":3}
run_result: {"type":"run_result","distance_km":5.2,"duration":"28:14","pace_avg":"5:26/км","start_time":"07:15","finish_time":"07:43","calories":420,"app":"...","insight":"..."}
equipment: {"type":"equipment","name":"...","muscle_group":"...","how_to_use":"...","sets_reps":"4×12"}
food: {"type":"food","name":"...","calories":450,"protein":30,"carbs":40,"fat":15,"assessment":"хорошо/норм/плохо","comment":"..."}

Для разговора — только текст."""

# ─── ДАННЫЕ ───────────────────────────────────────────────────────────────────
DEFAULT_EQUIPMENT = [
    {"name":"Турник высокий","muscle_group":"Широчайшие, бицепс","sets_reps":"5×макс"},
    {"name":"Брусья параллельные","muscle_group":"Грудь, трицепс","sets_reps":"5×12"},
    {"name":"Тяга горизонтальная","muscle_group":"Широчайшие, бицепс","sets_reps":"4×12"},
    {"name":"Наклонная скамья для пресса","muscle_group":"Пресс","sets_reps":"4×15"},
    {"name":"Наклонная скамья для жима","muscle_group":"Верхняя грудь, плечи","sets_reps":"4×12"},
    {"name":"Гиперэкстензия","muscle_group":"Поясница, ягодицы","sets_reps":"4×15"},
    {"name":"Жим ногами лёжа","muscle_group":"Квадрицепс, ягодицы","sets_reps":"4×12"},
    {"name":"Шведская стенка","muscle_group":"Пресс, сгибатели бедра","sets_reps":"3×15"},
    {"name":"Горизонтальная лестница","muscle_group":"Хват, широчайшие","sets_reps":"3×проход"},
]

def load() -> dict:
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except: pass
    return {
        "history": [], "runs": [], "food_log": [], "feeling_log": [],
        "weekly": {}, "monthly_km": {},
        "weight_log": [{"date": today_str(), "weight": 94}],
        "pending": None, "accepted": False,
        "last_run_km": 3.0, "streak": 0, "last_date": None,
        "prefs": [], "wishes": [],
        "equipment": DEFAULT_EQUIPMENT,
        "records": {"run": 0, "streak": 0, "min_weight": 94},
        "challenges": [], "achievements": [],
        "psych": {"times": [], "preferred": None, "ratings": [], "skips": []},
    }

def save(d: dict):
    try: DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e: log.error(f"Save: {e}")

def today_str(): return datetime.now(TIMEZONE).strftime("%Y-%m-%d")
def week_key():
    t = datetime.now(TIMEZONE); return (t - timedelta(days=t.weekday())).strftime("%Y-%W")
def month_key(): return datetime.now(TIMEZONE).strftime("%Y-%m")

def ctx(d: dict) -> str:
    r = d.get("records", {}); p = d.get("psych", {})
    w = d.get("weekly", {}).get(week_key(), {}); wl = d.get("weight_log", [{}])
    fl = d.get("feeling_log", [])
    parts = [f"[бег {d['last_run_km']} км | серия {d.get('streak',0)} дн."]
    if wl: parts.append(f"| вес {wl[-1].get('weight',94)} кг (до цели {wl[-1].get('weight',94)-80:.1f} кг)")
    runs = d.get("runs", [])
    if runs: parts.append(f"| последний бег {runs[-1].get('distance_km','?')} км")
    if w: parts.append(f"| неделя: {w.get('done',0)} тр. {w.get('km',0):.1f} км")
    if r.get("run"): parts.append(f"| рекорд {r['run']} км")
    if fl:
        lf = fl[-1]
        if lf.get("pain") and "нет" not in str(lf.get("pain","")): parts.append(f"| БОЛЬ: {lf['pain']} — избегай!")
    if p.get("ratings") and len(p["ratings"]) >= 2:
        avg = sum(p["ratings"][-5:]) / min(len(p["ratings"]),5)
        parts.append(f"| средняя оценка {avg:.1f}/5")
    if p.get("preferred"): parts.append(f"| выходит обычно: {p['preferred']}")
    wishes = d.get("wishes", [])
    if wishes: parts.append(f"| пожелания: {'; '.join(wishes[-2:])}")
    eq = [e["name"] for e in d.get("equipment", [])]
    if eq: parts.append(f"| тренажёры: {', '.join(eq)}")
    if d.get("prefs"): parts.append(f"| предпочтения: {'; '.join(d['prefs'][-3:])}")
    parts.append("]")
    return " ".join(parts)

# ─── ПОГОДА ───────────────────────────────────────────────────────────────────
def weather(hour: int = 7) -> dict:
    if not WEATHER_API_KEY: return {}
    try:
        url = f"https://api.openweathermap.org/data/2.5/forecast?q={urllib.parse.quote(CITY)}&appid={WEATHER_API_KEY}&units=metric&lang=ru&cnt=16"
        with urllib.request.urlopen(url, timeout=5) as r: raw = json.loads(r.read())
        tom = (datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
        best, bd = None, 999
        for it in raw.get("list", []):
            dt = it.get("dt_txt","")
            if not dt.startswith(tom): continue
            d = abs(int(dt[11:13]) - hour)
            if d < bd: bd, best = d, it
        if not best: return {}
        m = best["main"]; wnd = best.get("wind",{}); rain = best.get("rain",{}).get("3h",0); snow = best.get("snow",{}).get("3h",0)
        desc = best["weather"][0]["description"] if best.get("weather") else ""
        temp = round(m.get("temp",0)); feels = round(m.get("feels_like",0)); spd = round(wnd.get("speed",0))
        bad = rain > 0.5 or snow > 0 or spd > 10
        em = "🌧" if rain > 0.5 else "❄️" if snow else "💨" if spd > 10 else "⛅" if "облач" in desc else "☀️"
        return {"temp":temp,"feels":feels,"desc":desc,"speed":spd,"rain":rain,"bad":bad,"bike":not bad,
                "summary":f"{em} {temp}°C, {desc}, ветер {spd} м/с" + (f", дождь {rain:.1f}мм" if rain else "")}
    except Exception as e: log.warning(f"Weather: {e}"); return {}

def fmt_w(w: dict, ctx_str="утро") -> str:
    if not w: return ""
    lines = [f"\n🌤 *Погода на {ctx_str}:* {w['summary']}"]
    if abs(w.get("feels",0) - w.get("temp",0)) > 2: lines.append(f"   _↳ Ощущается {w['feels']}°C_")
    lines.append("   _↳ " + ("Велик не берём 🚫" if w.get("bad") else "Велик берём! 🚴_"))
    return "\n".join(lines)

# ─── ИЗОБРАЖЕНИЯ ──────────────────────────────────────────────────────────────
def img_to_b64(img_bytes: bytes, max_px=800) -> str:
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode not in ("RGB","L"): img = img.convert("RGB")
        if max(img.size) > max_px: img.thumbnail((max_px,max_px), Image.LANCZOS)
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e: log.error(f"img_to_b64: {e}"); return base64.b64encode(img_bytes).decode()

# ─── GROQ ─────────────────────────────────────────────────────────────────────
def ask(d: dict, msg: str, img: bytes = None, audio: bytes = None) -> str:
    context = ctx(d)
    try:
        if audio:
            t = groq.audio.transcriptions.create(file=("v.ogg",audio,"audio/ogg"), model=AUDIO_MODEL, language="ru")
            msg = t.text; log.info(f"Voice: {msg}")

        if img:
            b64 = img_to_b64(img)
            for attempt in range(3):
                try:
                    prompt = (f"{context}\nФото прислал пользователь. Определи что на нём:\n"
                              "- Скрин из приложения пробежки/велопробежки → JSON run_result\n"
                              "- Фото тренажёра на улице → JSON equipment\n"
                              "- Фото еды/блюда → JSON food\n"
                              "Ответь ТОЛЬКО чистым JSON без markdown.")
                    resp = groq.chat.completions.create(
                        model=VISION_MODEL,
                        messages=[{"role":"system","content":SYSTEM},
                                  {"role":"user","content":[{"type":"text","text":prompt},
                                   {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}]}],
                        max_tokens=600, temperature=0.1, timeout=20)
                    reply = resp.choices[0].message.content.strip()
                    reply = reply.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    if reply and ("{" in reply): return reply
                except Exception as e:
                    log.warning(f"Vision attempt {attempt+1}: {e}")
                    if attempt < 2: import time; time.sleep(2)
            return ""  # Vision не смог — вернём пустую строку

        hist = d.get("history",[])[-16:]
        msgs = ([{"role":"system","content":SYSTEM}] + hist +
                [{"role":"user","content":f"{context}\n{msg}"}])
        resp = groq.chat.completions.create(model=TEXT_MODEL, messages=msgs, max_tokens=700, temperature=0.7, timeout=30)
        reply = resp.choices[0].message.content.strip()
        hist.append({"role":"user","content":msg}); hist.append({"role":"assistant","content":reply[:400]})
        d["history"] = hist[-32:]; save(d)
        return reply
    except Exception as e:
        log.error(f"Groq error: {e}"); save(d); return ""

def pj(text: str) -> dict | None:
    if not text: return None
    try:
        s = text.find("{"); e = text.rfind("}")+1
        if s >= 0 and e > s: return json.loads(text[s:e])
    except: pass
    return None

# ─── ФОРМАТИРОВАНИЕ ───────────────────────────────────────────────────────────
def fmt_wo(w: dict) -> str:
    em = "🚴" if w.get("bike") else "🏃"
    lines = [f"{em} *{w['title']}*", f"⏱ {w.get('estimated_time','?')}"]
    lines.append("_🚲 Велик до площадки → тренировка → велик домой_\n" if w.get("bike") else "")
    for i,ex in enumerate(w.get("plan",[]),1):
        s,r = ex.get("sets",1),ex.get("reps","")
        line = f"{i}. {ex['exercise']} — " + (str(r) if s==1 else f"{s}×{r}")
        if ex.get("note"): line += f"\n   _↳ {ex['note']}_"
        lines.append(line)
    if w.get("motivation"): lines.append(f"\n💬 _{w['motivation']}_")
    if w.get("difficulty_target"): lines.append(f"🎯 _Сложность: {w['difficulty_target']}/5_")
    return "\n".join(lines)

def fmt_run(r: dict) -> str:
    L = ["🏃 *Зафиксировано!*\n"]
    if r.get("distance_km"): L.append(f"📏 *{r['distance_km']} км*")
    if r.get("duration"):    L.append(f"⏱ {r['duration']}")
    if r.get("pace_avg"):    L.append(f"⚡ Темп: {r['pace_avg']}")
    if r.get("start_time"):  L.append(f"🕐 Старт: {r['start_time']}")
    if r.get("finish_time"): L.append(f"🏁 Финиш: {r['finish_time']}")
    if r.get("calories"):    L.append(f"🔥 ~{r['calories']} ккал")
    if r.get("app"):         L.append(f"📱 {r['app']}")
    if r.get("insight"):     L.append(f"\n💡 _{r['insight']}_")
    return "\n".join(L)

def accept_kb(): return InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Принимаю!", callback_data="accept"),
    InlineKeyboardButton("💬 Изменить", callback_data="suggest"),
    InlineKeyboardButton("😴 Пропустить", callback_data="skip"),
]])
def rating_kb(): return InlineKeyboardMarkup([[
    InlineKeyboardButton("1",callback_data="r1"),InlineKeyboardButton("2",callback_data="r2"),
    InlineKeyboardButton("3",callback_data="r3"),InlineKeyboardButton("4",callback_data="r4"),
    InlineKeyboardButton("5",callback_data="r5"),
]])

def do_streak(d: dict):
    td = today_str()
    yd = (datetime.now(TIMEZONE)-timedelta(days=1)).strftime("%Y-%m-%d")
    last = d.get("last_date")
    s = (d.get("streak",0)+1) if last in (yd,td) else 1
    d["streak"] = s; d["last_date"] = td
    if s > d.get("records",{}).get("streak",0): d.setdefault("records",{})["streak"] = s

def add_km(d: dict, km: float) -> bool:
    wk,mk = week_key(), month_key()
    d.setdefault("weekly",{}).setdefault(wk,{"done":0,"skipped":0,"km":0})
    d["weekly"][wk]["km"] += km
    d.setdefault("monthly_km",{})[mk] = d["monthly_km"].get(mk,0)+km
    d["last_run_km"] = km
    rec = km > d.get("records",{}).get("run",0)
    if rec: d.setdefault("records",{})["run"] = km
    return rec

def check_ach(d: dict) -> list:
    new=[]; done=d.setdefault("achievements",[])
    r=d.get("records",{}); wl=d.get("weight_log",[{}]); w=wl[-1].get("weight",94)
    checks=[
        ("r1","🏅 Первая тренировка!", len(d.get("runs",[]))>=1),
        ("r5","🏅 Первые 5 км!", r.get("run",0)>=5),
        ("r7","🏅 7 км — уже серьёзно!", r.get("run",0)>=7),
        ("r10","🎉 10 КМ! ЦЕЛЬ ДОСТИГНУТА!",r.get("run",0)>=10),
        ("s7","🔥 7 дней подряд!",d.get("streak",0)>=7),
        ("s30","🔥 30 дней — железная воля!",d.get("streak",0)>=30),
        ("w90","⚖️ Меньше 90 кг!",w<90),("w85","⚖️ 85 кг!",w<85),("w80","🎉 80 КГ! ЦЕЛЬ!",w<=80),
    ]
    for k,msg,c in checks:
        if c and k not in done: done.append(k); new.append(msg)
    return new

# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "💪 *Тренер Макс — твой личный тренер*\n\n"
        "📷 Скрин пробежки → зафиксирую\n🍽 Фото еды → калории\n🎤 Голос → понимаю\n"
        "🌙 20:00 — план на завтра\n⏰ 6:30 — подъём!\n\n"
        "/workout — тренировка\n/done — выполнил\n/skip — пропустить\n"
        "/weight 93 — записать вес\n/stats — статистика\n"
        "/records — рекорды\n/challenge — челленджи\n/export — данные CSV",
        parse_mode="Markdown")

async def cmd_workout(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    wday = datetime.now(TIMEZONE).strftime("%A")
    warg = " ".join(c.args) if c.args else ""
    wth = weather(datetime.now(TIMEZONE).hour+1) if not warg else {}
    bike = wth.get("bike",True)
    wctx = warg or wth.get("summary","ясно")
    fl = d.get("feeling_log",[])
    pain = f"Боль: {fl[-1]['pain']} — исключи. " if fl and fl[-1].get("pain") and "нет" not in str(fl[-1].get("pain","")) else ""
    msg = await u.message.reply_text("⏳ Составляю...")
    reply = ask(d, f"Тренировка на сегодня ({wday}). Погода: {wctx}. {'Велик можно.' if bike else 'Велик НЕ берём.'} {pain}ТОЛЬКО JSON workout.")
    w = pj(reply)
    if w and w.get("type")=="workout":
        if not bike: w["bike"]=False
        d["pending"]=w; d["accepted"]=False; save(d)
        wstr = fmt_w(wth,"сейчас") if wth else ""
        await msg.edit_text((wstr+"\n\n"+fmt_wo(w)) if wstr else fmt_wo(w), parse_mode="Markdown", reply_markup=accept_kb())
    else:
        await msg.edit_text("Попробуй ещё раз /workout")

async def cmd_done(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    wk = week_key()
    d.setdefault("weekly",{}).setdefault(wk,{"done":0,"skipped":0,"km":0})["done"] += 1
    do_streak(d); ach = check_ach(d)
    cmt = " ".join(c.args) if c.args else ""
    reply = ask(d, f"Тренировка выполнена! {cmt} Похвали (1-2 предл.) и попроси оценку 1-5.")
    save(d)
    txt = f"🎉 *Засчитано! Серия: {d['streak']} дней* 🔥\n\n{reply or 'Отлично! Оцени сложность:'}"
    if ach: txt += "\n\n" + "\n".join(ach)
    await u.message.reply_text(txt, parse_mode="Markdown", reply_markup=rating_kb())

async def cmd_skip(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    wk = week_key()
    d.setdefault("weekly",{}).setdefault(wk,{"done":0,"skipped":0,"km":0})["skipped"] += 1
    d["streak"] = 0
    reason = " ".join(c.args) if c.args else "без причины"
    d.setdefault("psych",{}).setdefault("skips",[]).append({"date":today_str(),"reason":reason})
    reply = ask(d, f"Пропустил. Причина: {reason}. 1-2 предл. — пойми и мотивируй на завтра.")
    save(d)
    await u.message.reply_text(reply or "Окей, завтра выходим! 💪")

async def cmd_weight(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    if not c.args: await u.message.reply_text("Пример: /weight 93"); return
    try:
        wt = float(c.args[0].replace(",","."))
        d.setdefault("weight_log",[]).append({"date":today_str(),"weight":wt})
        lost = 94-wt; left = wt-80
        wl = d["weight_log"]
        plateau = len(wl)>=4 and max(x["weight"] for x in wl[-4:])-min(x["weight"] for x in wl[-4:])<0.5
        pctx = "Вес стоит уже 2+ недели — посоветуй что изменить. " if plateau else ""
        ach = check_ach(d)
        reply = ask(d, f"Вес: {wt} кг. Сброшено {lost:.1f} кг, до цели {left:.1f} кг. {pctx}Прокомментируй.")
        save(d)
        txt = f"⚖️ *{wt} кг*\n📉 Сброшено: {lost:.1f} кг | До цели: {left:.1f} кг\n\n{reply or ''}"
        if ach: txt += "\n\n"+"\n".join(ach)
        await u.message.reply_text(txt, parse_mode="Markdown")
    except: await u.message.reply_text("Пример: /weight 93")

async def cmd_stats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    w = d.get("weekly",{}).get(week_key(),{"done":0,"skipped":0,"km":0})
    wl = d.get("weight_log",[{}]); r = d.get("records",{})
    await u.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"*Неделя:* ✅{w['done']} тр. ❌{w['skipped']} пр. 🏃{w['km']:.1f} км\n"
        f"*Месяц:* {d.get('monthly_km',{}).get(month_key(),0):.1f} км\n\n"
        f"🔥 Серия: {d.get('streak',0)} дней\n"
        f"📏 Бег: {d['last_run_km']} км | Рекорд: {r.get('run',0)} км\n"
        f"⚖️ Вес: {wl[-1].get('weight','?')} кг\n"
        f"📋 Пробежек: {len(d.get('runs',[]))}\n"
        f"🏅 Достижений: {len(d.get('achievements',[]))}",
        parse_mode="Markdown")

async def cmd_records(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load(); r = d.get("records",{})
    runs = d.get("runs",[])
    paces = [x.get("pace_avg","") for x in runs if x.get("pace_avg")]
    await u.message.reply_text(
        f"🏆 *Рекорды*\n\n"
        f"🏃 Макс дистанция: *{r.get('run',0)} км*\n"
        f"⚡ Лучший темп: *{min(paces) if paces else 'нет данных'}*\n"
        f"🔥 Макс серия: *{r.get('streak',0)} дней*",
        parse_mode="Markdown")

async def cmd_challenge(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load()
    active = [x for x in d.get("challenges",[]) if not x.get("done")]
    if active:
        ch = active[0]
        cur = d.get("streak",0) if ch["type"]=="no_skip" else d.get("monthly_km",{}).get(month_key(),0)
        bar = "▓"*min(int(cur),ch["target"])+"░"*max(0,ch["target"]-int(cur))
        await u.message.reply_text(f"🎯 *{ch['name']}*\n\n{bar}\n{cur:.0f}/{ch['target']}", parse_mode="Markdown")
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 дней",callback_data="ch7"),
            InlineKeyboardButton("30 дней",callback_data="ch30"),
        ],[
            InlineKeyboardButton("50 км/мес",callback_data="ch50"),
            InlineKeyboardButton("100 км/мес",callback_data="ch100"),
        ]])
        await u.message.reply_text("🎯 *Выбери челлендж:*", parse_mode="Markdown", reply_markup=kb)

async def cmd_export(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = load(); out = io.StringIO()
    w = csv.writer(out); w.writerow(["Дата","Км","Темп","Время"])
    for r in d.get("runs",[]): w.writerow([r.get("timestamp","")[:10],r.get("distance_km",""),r.get("pace_avg",""),r.get("duration","")])
    await u.message.reply_document(document=io.BytesIO(out.getvalue().encode("utf-8-sig")),
        filename=f"тренировки_{month_key()}.csv", caption="📊 Твои данные")

# ─── ФОТО ─────────────────────────────────────────────────────────────────────
async def on_photo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_chat.id != YOUR_CHAT_ID: return
    msg = await u.message.reply_text("📷 Смотрю...")
    try:
        photo = u.message.photo[-1]
        f = await c.bot.get_file(photo.file_id)
        img_bytes = bytes(await f.download_as_bytearray())
        d = load()
        reply = ask(d, "", img_bytes)
        result = pj(reply)
    except Exception as e:
        log.error(f"Photo: {e}")
        await msg.edit_text("⚠️ Ошибка. Попробуй ещё раз или напиши данные текстом.")
        return

    if result and result.get("type")=="run_result":
        rec = {"ts":datetime.now(TIMEZONE).isoformat(), **{k:v for k,v in result.items() if k!="type"}}
        d.setdefault("runs",[]).append(rec)
        km = result.get("distance_km",0)
        is_rec = False
        if km:
            is_rec = add_km(d,km)
            d.setdefault("weekly",{}).setdefault(week_key(),{"done":0,"skipped":0,"km":0})["done"] += 1
        do_streak(d)
        if result.get("start_time"):
            h = int(str(result["start_time"]).split(":")[0]) if ":" in str(result.get("start_time","")) else 0
            label = "раннее утро" if h<9 else "утро" if h<12 else "день" if h<17 else "вечер"
            times = d.setdefault("psych",{}).setdefault("times",[])
            times.append(label)
            d["psych"]["preferred"] = max(set(times),key=times.count)
        ach = check_ach(d); save(d)
        txt = fmt_run(rec)
        if is_rec: txt += f"\n\n🏆 *Новый рекорд — {km} км!*"
        if ach: txt += "\n\n"+"\n".join(ach)
        txt += "\n\n*Оцени сложность (1-5):*"
        await msg.edit_text(txt, parse_mode="Markdown", reply_markup=rating_kb())

    elif result and result.get("type")=="equipment":
        name = result.get("name","Тренажёр")
        eq = d.setdefault("equipment",[])
        new = name not in [e.get("name") for e in eq]
        if new: eq.append({"name":name,"muscle_group":result.get("muscle_group",""),"sets_reps":result.get("sets_reps","4×12")}); save(d)
        txt = (f"💪 *{name}*\n🎯 {result.get('muscle_group','')}\n"
               f"📋 {result.get('how_to_use','')}\n🔢 {result.get('sets_reps','')}\n"
               f"\n{'✅ Добавил!' if new else '_Уже есть_'}")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Перестроить тренировку",callback_data="rebuild"),
            InlineKeyboardButton("➕ Ещё",callback_data="more_eq"),
        ]])
        await msg.edit_text(txt, parse_mode="Markdown", reply_markup=kb)

    elif result and result.get("type")=="food":
        d.setdefault("food_log",[]).append({"ts":datetime.now(TIMEZONE).isoformat(),**result})
        td = today_str()
        total_cal = sum(f.get("calories",0) for f in d["food_log"] if f.get("ts","")[:10]==td)
        save(d)
        ass = {"хорошо":"✅","норм":"👍","плохо":"⚠️"}.get(result.get("assessment","норм"),"👍")
        await msg.edit_text(
            f"{ass} *{result.get('name','Еда')}*\n"
            f"🔥 {result.get('calories',0)} ккал | 💪 {result.get('protein',0)}г белка\n"
            f"_{result.get('comment','')}_\n\n📊 Сегодня: ~{total_cal} ккал",
            parse_mode="Markdown")
    else:
        # Vision не сработал — умный fallback
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏃 Пробежка/велик", callback_data="ph_run"),
            InlineKeyboardButton("💪 Тренажёр", callback_data="ph_eq"),
            InlineKeyboardButton("🍽 Еда", callback_data="ph_food"),
        ]])
        await msg.edit_text(
            "Не смог распознать автоматически.\n\n"
            "_Или напиши текстом:_\n"
            "• «пробежал 5 км за 28 минут»\n"
            "• «добавь тренажёр: жим лёжа»",
            parse_mode="Markdown", reply_markup=kb)

# ─── ГОЛОС ────────────────────────────────────────────────────────────────────
async def on_voice(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_chat.id != YOUR_CHAT_ID: return
    msg = await u.message.reply_text("🎤 Слушаю...")
    try:
        f = await c.bot.get_file(u.message.voice.file_id)
        ab = bytes(await f.download_as_bytearray())
        d = load()
        t = groq.audio.transcriptions.create(file=("v.ogg",ab,"audio/ogg"),model=AUDIO_MODEL,language="ru")
        text = t.text
        await msg.edit_text(f"🎤 _{text}_", parse_mode="Markdown")
        reply = ask(d, text); save(d)
        wo = pj(reply)
        if wo and wo.get("type")=="workout":
            d["pending"]=wo; d["accepted"]=False; save(d)
            await u.message.reply_text(fmt_wo(wo), parse_mode="Markdown", reply_markup=accept_kb())
        elif reply:
            await u.message.reply_text(reply)
    except Exception as e:
        log.error(f"Voice: {e}"); await msg.edit_text("Не смог распознать голос.")

# ─── КНОПКИ ───────────────────────────────────────────────────────────────────
RATES = {1:"Вышел — уже победа. Следующая такая же.",2:"Легко — добавим нагрузки.",
         3:"Рабочий режим! Следующая чуть тяжелее.",4:"Огонь! Держим темп.",5:"Мощно! Следующая — восстановление."}

async def on_btn(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    d = load()

    if q.data.startswith("r") and q.data[1:].isdigit():
        n = int(q.data[1:])
        d.setdefault("psych",{}).setdefault("ratings",[]).append(n); save(d)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"Оценка {n}/5 ✓\n{RATES[n]}")

    elif q.data=="accept":
        d["accepted"]=True; save(d)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("🔥 *Принято! Вперёд!*\n\nКогда закончишь — /done и скрин 📷", parse_mode="Markdown")

    elif q.data=="suggest":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("💬 Расскажи что хочешь изменить!")

    elif q.data=="skip":
        d["streak"]=0
        d.setdefault("weekly",{}).setdefault(week_key(),{"done":0,"skipped":0,"km":0})["skipped"]+=1
        reply = ask(d,"Пропускает сегодня. 1 предл. — пойми и мотивируй на завтра."); save(d)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(reply or "Окей, завтра выходим! 💪")

    elif q.data=="rebuild":
        eq = "\n".join([f"- {e['name']} ({e['muscle_group']})" for e in d.get("equipment",[])])
        wday = datetime.now(TIMEZONE).strftime("%A")
        reply = ask(d, f"Составь тренировку на {wday} под эти тренажёры:\n{eq}\nТОЛЬКО JSON workout.")
        wo = pj(reply)
        if wo and wo.get("type")=="workout":
            d["pending"]=wo; d["accepted"]=False; save(d)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(f"🔄 *Под твою площадку:*\n\n{fmt_wo(wo)}", parse_mode="Markdown", reply_markup=accept_kb())

    elif q.data=="more_eq":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("📷 Скидывай следующее фото тренажёра!")

    elif q.data in ("ph_run","ph_eq","ph_food"):
        hints = {"ph_run":"Напиши: «пробежал 5 км за 28 минут, старт в 7:15»",
                 "ph_eq":"Напиши: «добавь тренажёр: жим лёжа, грудь, 4×10»",
                 "ph_food":"Напиши: «гречка с курицей 300г»"}
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(hints[q.data])

    elif q.data in ("ch7","ch30","ch50","ch100"):
        chmap = {"ch7":{"name":"7 дней подряд","type":"no_skip","target":7},
                 "ch30":{"name":"30 дней подряд","type":"no_skip","target":30},
                 "ch50":{"name":"50 км за месяц","type":"km","target":50},
                 "ch100":{"name":"100 км за месяц","type":"km","target":100}}
        ch = chmap[q.data]; d.setdefault("challenges",[]).append({**ch,"done":False,"started":today_str()}); save(d)
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"🎯 *Челлендж: {ch['name']}!*\n/challenge — прогресс", parse_mode="Markdown")

# ─── ЧАТ ──────────────────────────────────────────────────────────────────────
async def on_msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_chat.id != YOUR_CHAT_ID: return
    d = load(); text = u.message.text; low = text.lower()

    # Предпочтения
    for kw in ["не люблю","терпеть не могу","обожаю","люблю","предпочитаю"]:
        if kw in low and len(text)<120:
            if text not in d.setdefault("prefs",[]): d["prefs"].append(text)

    # Пожелания на завтра
    if any(h in low for h in ["завтра","хочу побегать","хочу бег","хочу площадку","планирую","собираюсь"]):
        d.setdefault("wishes",[]).append(text); save(d)
        reply = ask(d, f"Пользователь: '{text}'. Ответь тепло, скажи что учтёшь в плане в 20:00. НЕ присылай тренировку.")
        await u.message.reply_text(reply or "Запомнил! Учту в вечернем плане 👍"); return

    # Боль/самочувствие
    if any(h in low for h in ["болит","боль","ломит","колено","спина","плечо","не могу"]):
        d.setdefault("feeling_log",[]).append({"ts":datetime.now(TIMEZONE).isoformat(),"pain":text[:100]}); save(d)
        reply = ask(d, f"Пользователь о самочувствии: '{text}'. Уточни, запомни. НЕ присылай тренировку.")
        await u.message.reply_text(reply or "Понял, учту при составлении тренировки."); return

    # Добавление тренажёра текстом
    if any(h in low for h in ["добавь тренажёр","добавь тренажер","добавь снаряд"]):
        reply = ask(d, f"Пользователь просит добавить тренажёр: '{text}'. Распарси и ответь JSON equipment.")
        eq = pj(reply)
        if eq and eq.get("type")=="equipment":
            name = eq.get("name","")
            eq_list = d.setdefault("equipment",[])
            if name and name not in [e.get("name") for e in eq_list]:
                eq_list.append({"name":name,"muscle_group":eq.get("muscle_group",""),"sets_reps":eq.get("sets_reps","4×12")}); save(d)
                await u.message.reply_text(f"✅ Добавил: *{name}*", parse_mode="Markdown")
            else:
                await u.message.reply_text(f"_{name} уже есть в программе_", parse_mode="Markdown")
        else:
            await u.message.reply_text("Не понял. Напиши например: «добавь тренажёр: жим лёжа, грудь, 4×10»")
        return

    # Ручной ввод пробежки
    km_m = re.search(r'(\d+[.,]?\d*)\s*км', low)
    if km_m and any(w in low for w in ["пробежал","побежал","бежал","пробежка","пробежку"]):
        km = float(km_m.group(1).replace(",",".")); rec = {"ts":datetime.now(TIMEZONE).isoformat(),"distance_km":km,"manual":True}
        tm = re.search(r'(\d+)\s*мин', low)
        if tm:
            mins = int(tm.group(1)); rec["duration"] = f"0:{mins:02d}" if mins<60 else f"{mins//60}:{mins%60:02d}"
            if km>0: ps = int(mins*60/km); rec["pace_avg"] = f"{ps//60}:{ps%60:02d}/км"
        d.setdefault("runs",[]).append(rec); is_rec=add_km(d,km); do_streak(d)
        d.setdefault("weekly",{}).setdefault(week_key(),{"done":0,"skipped":0,"km":0})["done"]+=1
        ach=check_ach(d); save(d)
        reply = ask(d, f"Зафиксировал пробежку {km} км. Похвали коротко и попроси оценку 1-5.")
        txt = f"✅ *{km} км зафиксировано!*" + (" 🏆 Новый рекорд!" if is_rec else "")
        if ach: txt += "\n"+"\n".join(ach)
        txt += f"\n\n{reply or 'Оцени сложность:'}"
        await u.message.reply_text(txt, parse_mode="Markdown", reply_markup=rating_kb()); return

    # Обычный разговор — строго запрещаем JSON и тренировки
    reply = ask(d,
        f"Пользователь пишет: '{text}'\n"
        "Ответь как живой тренер — с характером, коротко (2-4 предложения). "
        "Вступай в диалог, задавай вопрос если нужно. "
        "ЗАПРЕЩЕНО: присылать тренировку, использовать JSON, писать 'финальный пришлю в 20:00'. "
        "Просто поговори."
    )
    save(d)
    # Если вдруг пришёл JSON — не показываем, отвечаем заглушкой
    if reply and reply.strip().startswith("{"):
        await u.message.reply_text("Понял! Если хочешь тренировку прямо сейчас — напиши /workout 💪")
    elif reply:
        await u.message.reply_text(reply)
    else:
        await u.message.reply_text("Не понял, попробуй ещё раз.")

# ─── ПЛАНИРОВЩИК ──────────────────────────────────────────────────────────────
async def job_wakeup(app):
    try:
        d = load(); wo = d.get("pending"); wth = weather(7); wstr = fmt_w(wth,"сегодня")
        if d.get("accepted") and wo:
            txt = f"🌅 *6:30 — Подъём!* 💪{wstr}\n\nТы принял тренировку — вперёд!\n\n{fmt_wo(wo)}\n\n_Закончишь — /done и скрин 📷_"
            await app.bot.send_message(YOUR_CHAT_ID, txt, parse_mode="Markdown")
        elif wo:
            txt = f"🌅 *6:30 — Подъём!* 💪{wstr}\n\nЗадание на сегодня:\n\n{fmt_wo(wo)}"
            await app.bot.send_message(YOUR_CHAT_ID, txt, parse_mode="Markdown", reply_markup=accept_kb())
        else:
            await app.bot.send_message(YOUR_CHAT_ID, f"🌅 *6:30 — Подъём!* 💪{wstr}\n\n/workout — получить задание", parse_mode="Markdown")
    except Exception as e: log.error(f"job_wakeup: {e}")

async def job_evening(app):
    try:
        d = load()
        wday = (datetime.now(TIMEZONE)+timedelta(days=1)).strftime("%A")
        wth = weather(7); bike = wth.get("bike",True); wstr = fmt_w(wth,"завтрашнее утро")
        wctx = f"Погода завтра: {wth.get('summary','')}. {'Велик берём.' if bike else 'Велик НЕ берём.'} " if wth else ""
        wishes = d.get("wishes",[]); wsh = f"Пожелания: {'; '.join(wishes[-2:])}. Учти! " if wishes else ""
        if wishes: d["wishes"] = []
        fl = d.get("feeling_log",[]); pain = f"Боль: {fl[-1]['pain']} — исключи. " if fl and fl[-1].get("pain") and "нет" not in str(fl[-1].get("pain","")) else ""
        reply = ask(d, f"Составь план на завтра ({wday}). {wctx}{wsh}{pain}ТОЛЬКО JSON workout.")
        wo = pj(reply)
        if wo and wo.get("type")=="workout":
            if not bike: wo["bike"]=False
            d["pending"]=wo; d["accepted"]=False; save(d)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Хочу изменить",callback_data="suggest")]])
            await app.bot.send_message(YOUR_CHAT_ID,
                f"🌙 *План на завтра:*{wstr}\n\n{fmt_wo(wo)}\n\n_Утром в 6:30 — с кнопками принятия._",
                parse_mode="Markdown", reply_markup=kb)
        else:
            log.error(f"job_evening failed, reply: {reply[:100] if reply else 'empty'}")
            # Fallback — простое сообщение
            await app.bot.send_message(YOUR_CHAT_ID, "🌙 Напиши /workout чтобы получить задание на завтра.")
    except Exception as e: log.error(f"job_evening: {e}")

async def job_motivation(app):
    try:
        d = load()
        if d.get("last_date") != today_str():
            reply = ask(d,"Не тренировался сегодня. Подстегни — 1-2 предл., учти психологию пользователя.")
            if reply: await app.bot.send_message(YOUR_CHAT_ID, f"⚡ {reply}")
    except Exception as e: log.error(f"job_motivation: {e}")

async def job_weekly(app):
    try:
        d = load(); stats = d.get("weekly",{}); keys = sorted(stats.keys())
        tw = stats.get(week_key(),{"done":0,"skipped":0,"km":0})
        pw = stats.get(keys[-2],{}) if len(keys)>=2 else {}
        cmp = ""
        if pw:
            dk=tw.get("km",0)-pw.get("km",0); dd=tw.get("done",0)-pw.get("done",0)
            cmp = f"\n*Vs прошлая неделя:* {'📈' if dk>=0 else '📉'}{'+' if dk>=0 else ''}{dk:.1f} км  {'📈' if dd>=0 else '📉'}{dd:+d} тр.\n"
        reply = ask(d,
            f"Итоги: {tw.get('done',0)} тр., {tw.get('skipped',0)} пр., {tw.get('km',0):.1f} км, серия {d.get('streak',0)} дн."
            +(f" Прошлая: {pw.get('km',0):.1f} км." if pw else "")
            +" Анализ (3-4 предл.) + цель на след. неделю.")
        await app.bot.send_message(YOUR_CHAT_ID,
            f"📊 *ИТОГИ НЕДЕЛИ*\n\n✅{tw.get('done',0)} тр. ❌{tw.get('skipped',0)} пр. 🏃{tw.get('km',0):.1f} км 🔥{d.get('streak',0)} дн.\n{cmp}\n{reply or ''}",
            parse_mode="Markdown")
    except Exception as e: log.error(f"job_weekly: {e}")

async def job_weight(app):
    try:
        d = load(); wl = d.get("weight_log",[{}])
        await app.bot.send_message(YOUR_CHAT_ID,
            f"⚖️ *Понедельник — взвешивание!*\nПоследний вес: {wl[-1].get('weight','?')} кг\n\n/weight 93.5",
            parse_mode="Markdown")
    except Exception as e: log.error(f"job_weight: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("workout",   cmd_workout))
    app.add_handler(CommandHandler("done",      cmd_done))
    app.add_handler(CommandHandler("skip",      cmd_skip))
    app.add_handler(CommandHandler("weight",    cmd_weight))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("records",   cmd_records))
    app.add_handler(CommandHandler("challenge", cmd_challenge))
    app.add_handler(CommandHandler("export",    cmd_export))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))

    sched = AsyncIOScheduler(timezone=TIMEZONE)
    sched.add_job(job_wakeup,    "cron", hour=6,  minute=30, args=[app], misfire_grace_time=600)
    sched.add_job(job_motivation,"cron", hour=14, minute=0,  args=[app], misfire_grace_time=600)
    sched.add_job(job_evening,   "cron", hour=20, minute=0,  args=[app], misfire_grace_time=600)
    sched.add_job(job_weekly,    "cron", day_of_week="sun", hour=19, args=[app], misfire_grace_time=600)
    sched.add_job(job_weight,    "cron", day_of_week="mon", hour=9,  args=[app], misfire_grace_time=600)
    sched.start()

    log.info("✅ Тренер Макс v4.0 запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
