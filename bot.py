"""
DTEK Kyiv-region outage bot - Using dtek-blackout-client for reliable schedule fetching.
"""
import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta

import pytz
import redis
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# The key library that replaces manual scraping
from dtek_client import DtekClient
from dtek_client.enums import SlotStatus  # For mapping status to emoji

load_dotenv()

# ============================= CONFIG =============================
TOKEN = os.getenv("BOT_TOKEN")

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 17778))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

GROUP = os.getenv("GROUP", "GPV4.1")
REGION = "krem"  # 'krem' = DTEK Kyiv Regional Networks (smaller towns)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
CACHE_TTL = 90
DEFAULT_ATTENTION = "Актуальних повідомлень немає."
TZ = pytz.timezone("Europe/Kiev")

# Offset users (unchanged)
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

logging.basicConfig(level=logging.INFO)

# Cache for snapshots (now holds data from the library)
_cache = {"ts": 0.0, "snap": None}
fetch_lock = asyncio.Lock()

# A single DtekClient instance can be reused. It will manage its own Playwright browser for WAF bypass.
dtek_client: DtekClient | None = None


async def get_dtek_client() -> DtekClient:
    """Lazily initializes and returns the DtekClient."""
    global dtek_client
    if dtek_client is None:
        # The client will handle WAF bypass internally using Playwright.
        dtek_client = DtekClient(REGION)
    return dtek_client


def now_kyiv():
    return datetime.now(TZ)


async def rcall(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ============================= SCHEDULE FETCHING (LIBRARY-BASED) =============================
async def fetch_schedules_from_library() -> dict:
    """
    Fetches today's and tomorrow's confirmed schedules for the configured group
    using the dtek-blackout-client library.
    """
    client = await get_dtek_client()

    # We need to find an address that belongs to the target group.
    # The library allows searching by address. For a group-based bot,
    # we need a representative address. A common approach is to use the
    # city/street/house that you know belongs to GPV4.1 in your region.
    # **IMPORTANT**: You must replace these with a real address from your area
    # that is assigned to GROUP (GPV4.1). The library's stub client can help you
    # test with a known address (e.g., "м. Українка", "вул. Юності", "1").
    # For production, use an address that actually maps to your group.
    # You can use `client.get_streets` and `client.get_home_num` to discover
    # the correct group_id for an address.

    # Example using an address known to be in GPV4.1 from the library's stub data.
    # **You must verify this address is correct for your specific region and group.**
    city = "м. Українка"
    street = "вул. Юності"
    house_number = "1"

    try:
        today_slots = await client.get_today_schedule(city, street, house_number)
        tomorrow_slots = await client.get_tomorrow_schedule(city, street, house_number)
    except Exception as e:
        logging.error(f"Failed to fetch schedules from library: {e}")
        raise

    now = now_kyiv()
    today_date = now.date()
    tomorrow_date = today_date + timedelta(days=1)

    # Convert the library's SlotStatus dict into the format our bot uses.
    def slots_to_internal(slot_dict: dict[str, SlotStatus]) -> dict:
        """Converts a dict of time->SlotStatus into a list of emojis and a signature string."""
        # The dict keys are times like "00:00", "00:30", etc.
        # We sort them to ensure correct order.
        sorted_times = sorted(slot_dict.keys())
        emojis = []
        for t in sorted_times:
            status = slot_dict[t]
            # Use the .has_outage property to decide: True = outage (red), False = power (green)
            emojis.append("🔴" if status.has_outage else "🟢")

        return {
            "date": None,  # Will be set outside
            "slots": emojis,
            "sig": "".join(emojis),
        }

    schedules = {}

    if today_slots:
        internal = slots_to_internal(today_slots)
        internal["date"] = today_date
        schedules[int(today_date.strftime("%s"))] = internal

    if tomorrow_slots:
        internal = slots_to_internal(tomorrow_slots)
        internal["date"] = tomorrow_date
        schedules[int(tomorrow_date.strftime("%s"))] = internal

    return schedules


async def get_snapshot(force=False):
    async with fetch_lock:
        if not force and _cache["snap"] and (time.monotonic() - _cache["ts"] < CACHE_TTL):
            return _cache["snap"]

        try:
            schedules = await fetch_schedules_from_library()

            if not schedules:
                raise RuntimeError(f"Group {GROUP} not found in fetched schedules.")

            # The library does not provide the "attention" text.
            # We will use a default or you can implement a separate lightweight scraper
            # for the attention banner if it is critical.
            attention = DEFAULT_ATTENTION

            snap = {
                "schedules": schedules,
                "updated": now_kyiv().strftime("%Y-%m-%d %H:%M:%S"),
                "attention": attention,
            }
            _cache["ts"] = time.monotonic()
            _cache["snap"] = snap
            return snap
        except Exception as e:
            # If a fresh fetch fails but we have a cached snapshot, return the cached one.
            if _cache["snap"]:
                logging.warning("⚠️ Failed to fetch fresh data, using cache: %s", e)
                return _cache["snap"]
            raise e


# ============================= TIME CALC & RENDERING (ADAPTED) =============================
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


def day_title(d, now) -> str:
    delta = (d - now.date()).days
    label = {0: "на сегодня", 1: "на завтра"}.get(delta, "на")
    return f"{label} {d.strftime('%d.%m.%Y')}"


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
                    old = await rcall(redis_client.get, key)

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
            logging.warning("⚠️ Monitoring skipped due to error: %s", e)
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
        except Exception:
            await m.answer("❌ <b>Ошибка:</b> график временно недоступен, попробуйте через минуту.", parse_mode="HTML")
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
        # The DtekClient manages its own resources; we don't need to stop a global browser here.


if __name__ == "__main__":
    asyncio.run(main())
