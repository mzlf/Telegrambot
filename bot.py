"""
DTEK Kyiv-region outage bot - Reliable JSON mirror version.
"""
import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta

import aiohttp
import pytz
import redis
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# ============================= CONFIG =============================
TOKEN = os.getenv("BOT_TOKEN")

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 17778))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# Group name must match exactly what is in the JSON (e.g., "GPV4.1")
GROUP = os.getenv("GROUP", "GPV4.1")

# Verified working URL for Kyiv Regional Network data
DATA_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json"

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
CACHE_TTL = 90
DEFAULT_ATTENTION = "Актуальних повідомлень немає."
TZ = pytz.timezone("Europe/Kiev")

OFFSET_USERS = {}
env_offsets = os.getenv("OFFSET_USERS")
if env_offsets:
    try:
        for pair in env_offsets.split(","):
            uid_str, offset_str = pair.split(":")
            OFFSET_USERS[int(uid_str.strip())] = int(offset_str.strip())
    except Exception as e:
        logging.error("Failed to parse OFFSET_USERS from env: %s", e)
else:
    OFFSET_USERS = {
        1437149670: 1,
        6072460838: -3
    }

bot = Bot(token=TOKEN)
dp = Dispatcher()

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    username="default",
    password=REDIS_PASSWORD,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Cache
fetch_lock = asyncio.Lock()
_cache = {"ts": 0.0, "snap": None}


def now_kyiv():
    return datetime.now(TZ)


def _s(x):
    return x.decode() if isinstance(x, bytes) else x


async def rcall(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ============================= PARSING (FROM JSON MIRROR) =============================
def day_to_slots(day: dict) -> list:
    """Converts a day's schedule dict to a list of 48 emoji slots."""
    slots = []
    for h in range(1, 25):
        v = day.get(str(h), "yes")
        # "no", "first", and "second" all mean the power is OFF (red)
        slots.append("🔴" if v in ("no", "first", "second") else "🟢")
        slots.append("🔴" if v in ("no", "first", "second") else "🟢")
    return slots


def parse_schedules_from_fact(fact: dict) -> dict:
    """Parses the 'fact' object from the mirror JSON."""
    result = {}
    data_block = fact.get("data") or {}
    for ts, groups in data_block.items():
        day = groups.get(GROUP)
        if not day:
            # Log which groups are available once, to help debug if GROUP is wrong
            if not result:
                logging.debug("Available groups in first timestamp: %s", list(groups.keys())[:5])
            continue
        slots = day_to_slots(day)
        result[int(ts)] = {
            "date": datetime.fromtimestamp(int(ts), TZ).date(),
            "slots": slots,
            "sig": "".join(slots),
        }
    return dict(sorted(result.items()))


def day_title(d, now) -> str:
    delta = (d - now.date()).days
    label = {0: "на сегодня", 1: "на завтра"}.get(delta, "на")
    return f"{label} {d.strftime('%d.%m.%Y')}"


# ============================= FETCHING & CACHE =============================
async def fetch_fact_from_mirror() -> dict:
    """Fetches the entire JSON blob from the public GitHub mirror."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; OutageBot/1.0)"}
    async with aiohttp.ClientSession() as session:
        async with session.get(DATA_URL, timeout=20, headers=headers) as resp:
            logging.info("Mirror HTTP status: %s", resp.status)
            resp.raise_for_status()
            data = await resp.json()
            fact = data.get("fact")
            if not fact or "data" not in fact:
                raise RuntimeError("Invalid 'fact' structure in mirror JSON.")
            return fact


async def get_snapshot(force=False):
    async with fetch_lock:
        if not force and _cache["snap"] and (time.monotonic() - _cache["ts"] < CACHE_TTL):
            return _cache["snap"]

        try:
            fact = await fetch_fact_from_mirror()
            schedules = parse_schedules_from_fact(fact)

            if not schedules:
                raise RuntimeError(f"Group {GROUP} not found in mirror data. Check your GROUP env variable.")

            snap = {
                "schedules": schedules,
                "updated": fact.get("updateFact", "---"),
                "attention": DEFAULT_ATTENTION,
            }
            _cache["ts"] = time.monotonic()
            _cache["snap"] = snap
            logging.info("✅ Snapshot updated. Group %s has %d days of data.", GROUP, len(schedules))
            return snap
        except Exception as e:
            if _cache["snap"]:
                logging.warning("⚠️ Mirror fetch failed, using cached snapshot: %s", e)
                return _cache["snap"]
            # Re-raise so the caller (manual handler) can log and notify the user
            raise e


# ============================= TIME CALC & RENDERING =============================
def h_str(h):
    return str(int(h)) if h % 1 == 0 else str(h)


def format_time(h, m):
    if h == 0 and m == 0:
        return "меньше минуты"
    if h == 0:
        return f"{m} мин"
    if m == 0:
        return f"{h}ч"
    return f"{h}ч {m} мин"


def slots_to_text(slots: list, hour_offset: int = 0) -> str:
    def fmt(i):
        slot_index = (i + hour_offset * 2) % 48
        m = slot_index * 30
        return f"{(m // 60) % 24:02d}:{m % 60:02d}"

    lines, start = [], 0
    for i in range(1, 49):
        if i == 48 or slots[i] != slots[start]:
            if slots[start] == "🔴":
                end = fmt(48) if i == 48 else fmt(i)
                lines.append(f"🔴{fmt(start)} — {end}🔴")
            start = i
    return "\n".join(lines) or "🟢 Отключений нет"


def calculate_time_left(schedules: dict) -> str:
    now = now_kyiv()
    by_date = {d["date"]: d for d in schedules.values()}

    today = by_date.get(now.date())
    if not today:
        return "⚠️ <b>На сайте пока нет графика на сегодня.</b>\n"
    tomorrow = by_date.get(now.date() + timedelta(days=1))

    timeline = today["slots"] + (tomorrow["slots"] if tomorrow else [])
    minutes_now = now.hour * 60 + now.minute
    idx = minutes_now // 30
    current = timeline[idx] if idx < len(timeline) else "🟢"

    events, state = [], current
    for i in range(idx + 1, len(timeline)):
        if timeline[i] != state:
            diff = i * 30 - minutes_now
            label = "<b>До включения</b>" if timeline[i] == "🟢" else "<b>До выключения</b>"
            events.append(f" ↳ {label}: {format_time(diff // 60, diff % 60)}")
            state = timeline[i]
            if len(events) >= 2:
                break

    status = "🔋 Свет есть" if current == "🟢" else "🪫 Света нет"
    res = "💡 <b>ИНФОРМАЦИЯ ПО СЕТИ</b>\n"
    res += f" ↳ <b>Сейчас</b>: {status}\n"
    res += ("\n".join(events) + "\n") if events else " ↳ ✨<b>Отключений не планируется</b>\n"

    res += "📊 <b>Статистика</b>\n"
    res += (f" ↳ <b>Сегодня</b>: 🔋 {h_str(today['slots'].count('🟢') / 2)} ч | "
            f"🪫 {h_str(today['slots'].count('🔴') / 2)} ч\n")
    if tomorrow:
        res += (f" ↳ <b>Завтра</b>: 🔋 {h_str(tomorrow['slots'].count('🟢') / 2)} ч | "
                f"🪫 {h_str(tomorrow['slots'].count('🔴') / 2)} ч\n")
    return res


def render_message(snap: dict, header: str, user_id=None) -> str:
    now = now_kyiv()
    offset = OFFSET_USERS.get(int(user_id), 0) if user_id else 0
    display_now = now + timedelta(hours=offset)

    by_date = {d["date"]: d for d in snap["schedules"].values()}
    today_date = display_now.date()
    tomorrow_date = today_date + timedelta(days=1)

    today = by_date.get(today_date)
    tomorrow = by_date.get(tomorrow_date)

    blocks = []

    if today:
        text = slots_to_text(today['slots'], hour_offset=offset)
        blocks.append(f"⚡{day_title(today['date'], display_now)}⚡\n{text}")
    else:
        blocks.append(f"⚡на сегодня {today_date.strftime('%d.%m.%Y')}⚡\n⚠️ График на сегодня отсутствует")

    if tomorrow:
        text = slots_to_text(tomorrow['slots'], hour_offset=offset)
        blocks.append(f"⚡{day_title(tomorrow['date'], display_now)}⚡\n{text}")
    else:
        blocks.append(f"⚡на завтра {tomorrow_date.strftime('%d.%m.%Y')}⚡\n⚠️ График на завтра отсутствует, нужно подождать пока появится")

    blocks_str = "\n\n".join(blocks)

    return (
        f"{header}\n"
        f"<pre>{blocks_str}</pre>\n"
        f"{calculate_time_left(snap['schedules'])}"
        f"<pre>🕒 Обновлено: {html.escape(snap['updated'])}</pre>"
    )


# ============================= UI HELPERS =============================
async def delete_message_after(message: types.Message, sleep_time: int):
    await asyncio.sleep(sleep_time)
    try:
        await message.delete()
    except Exception:
        pass


def get_kb():
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="⚡ Показать график")],
                  [types.KeyboardButton(text="🔔 Мониторинг")]],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие в меню 👇",
    )


# ============================= MONITORING =============================
async def monitoring_task():
    while True:
        try:
            users = await rcall(redis_client.smembers, "monitoring_users")
            if users:
                snap = await get_snapshot(force=True)

                changed = False
                for ts, d in snap["schedules"].items():
                    key = f"sched2:{GROUP}:{ts}"
                    old = _s(await rcall(redis_client.get, key))

                    if old is None or old != d["sig"]:
                        changed = True

                    await rcall(redis_client.set, key, d["sig"], ex=172800)

                if changed:
                    logging.info("📢 Schedule changed or new day added, notifying users")
                    builder = InlineKeyboardBuilder()
                    builder.row(types.InlineKeyboardButton(
                        text="🔹 Информация на сегодня", callback_data="show_att"))

                    for uid in users:
                        try:
                            uid_int = int(uid)
                            msg = render_message(snap, "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ</b>", user_id=uid_int)
                            await bot.send_message(uid_int, msg,
                                                   reply_markup=builder.as_markup(),
                                                   parse_mode="HTML")
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logging.error("Broadcast to %s failed: %s", uid, e)
        except Exception as e:
            logging.warning("⚠️ Monitoring loop error (will retry): %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


# ============================= HANDLERS =============================
@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот запущен.", reply_markup=get_kb())


@dp.message(F.text.contains("график") | F.text.contains("Показать"))
async def manual(m: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="📢 Уведомление Укрэнерго", callback_data="show_att"))

    snap = _cache["snap"]
    if not snap:
        try:
            snap = await get_snapshot()
        except Exception as e:
            # CRITICAL: Log the real error so you can see it in the console
            logging.exception("Manual schedule fetch failed for user %s", m.from_user.id)
            await m.answer("❌ <b>Ошибка:</b> график временно недоступен. Проверьте логи бота для деталей.", parse_mode="HTML")
            return

    await m.answer(render_message(snap, "💡<b>ГРАФИК ОТКЛЮЧЕНИЙ</b>", user_id=m.from_user.id),
                   reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "show_att")
async def callback_att(call: types.CallbackQuery):
    snap = _cache["snap"]
    text = snap["attention"] if snap else "Информация отсутствует."
    info_msg = await call.message.answer(
        f"📢 <b>Уведомление Укрэнерго:</b>\n\n{html.escape(text)}", parse_mode="HTML")
    await call.answer()
    asyncio.create_task(delete_message_after(info_msg, 120))


@dp.message(F.text.contains("мониторинг") | F.text.contains("Мониторинг") | F.text.contains("🔔"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if await rcall(redis_client.sismember, "monitoring_users", uid):
        await rcall(redis_client.srem, "monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        await rcall(redis_client.sadd, "monitoring_users", uid)
        await m.answer("Мониторинг включен.")


@dp.message(F.text)
async def default_handler(m: types.Message):
    await m.answer("Пожалуйста, выберите действие из меню ниже:", reply_markup=get_kb())


# ============================= WEB SERVER FOR UPTIMEROBOT =============================
async def handle_ping(request):
    return web.Response(text="Bot is alive and running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 HTTP Ping server started on port {port}")


# ============================= MAIN =============================
async def warm_up():
    try:
        await get_snapshot(force=True)
        logging.info("✅ Warm-up done")
    except Exception:
        logging.exception("Warm-up failed (will retry on demand)")


async def main():
    background = [
        asyncio.create_task(warm_up()),
        asyncio.create_task(monitoring_task()),
        asyncio.create_task(start_web_server())
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in background:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
