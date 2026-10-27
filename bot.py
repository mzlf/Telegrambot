"""
DTEK Kyiv-region outage bot - Optimized, Anti-Bot, Web-Server for UptimeRobot.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import pytz
import redis
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright

# ============================= CONFIG =============================
TOKEN = "8699245998:AAFrajfnUyi7lVNdPvzrykAz6KjWcgAtXHw"

REDIS_HOST = "wrench-microstrong-back-61275.db.redis.io"
REDIS_PORT = 17778
REDIS_PASSWORD = "ZZjNkOsY6RK6iojvs721XY8sYB8c4fHM"

GROUP = "GPV4.1"
SITE_URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
SITE_HOST = "dtek-krem.com.ua"
HEADLESS = os.getenv("HEADLESS", "1") == "1"
CHECK_INTERVAL = 300
CACHE_TTL = 90                     # Кэш на 15 минут (чтобы пользователи получали график мгновенно)
DEFAULT_ATTENTION = "Актуальных повідомлень немає."
TZ = pytz.timezone("Europe/Kiev")

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

# browser state
playwright = None
browser = None
context = None
fetch_lock = asyncio.Lock()
_cache = {"ts": 0.0, "snap": None}


def now_kyiv():
    return datetime.now(TZ)


def _s(x):
    return x.decode() if isinstance(x, bytes) else x


async def rcall(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ============================= PARSING =============================
def extract_fact_from_html(page_html: str):
    m = re.search(r"DisconSchedule\.fact\s*=\s*", page_html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(page_html[m.end():])
        return obj
    except ValueError:
        return None


def extract_attention_from_html(page_html: str) -> str:
    m = re.search(r'class="[^"]*m-attention__text[^"]*"[^>]*>(.*?)</div>', page_html, re.S)
    if not m:
        return DEFAULT_ATTENTION
    text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    return text or DEFAULT_ATTENTION


def day_to_slots(day: dict) -> list:
    slots = []
    for h in range(1, 25):
        v = day.get(str(h), "yes")
        slots.append("🔴" if v in ("no", "first") else "🟢")
        slots.append("🔴" if v in ("no", "second") else "🟢")
    return slots


def slots_to_text(slots: list) -> str:
    def fmt(i):
        m = i * 30
        return f"{m // 60:02d}:{m % 60:02d}"

    lines, start = [], 0
    for i in range(1, 49):
        if i == 48 or slots[i] != slots[start]:
            if slots[start] == "🔴":
                end = "00:00" if i == 48 else fmt(i)
                lines.append(f"🔴{fmt(start)} — {end}🔴")
            start = i
    return "\n".join(lines) or "🟢 Отключений нет"


def parse_schedules(fact: dict) -> dict:
    result = {}
    for ts, groups in (fact.get("data") or {}).items():
        day = groups.get(GROUP)
        if not day:
            continue
        slots = day_to_slots(day)
        result[int(ts)] = {
            "date": datetime.fromtimestamp(int(ts), TZ).date(),
            "slots": slots,
            "text": slots_to_text(slots),
            "sig": "".join(slots),
        }
    return dict(sorted(result.items()))


def day_title(d, now) -> str:
    delta = (d - now.date()).days
    label = {0: "на сегодня", 1: "на завтра"}.get(delta, "на")
    return f"{label} {d.strftime('%d.%m.%Y')}"


# ============================= BROWSER =============================
async def start_browser():
    global playwright, browser, context
    if browser:
        return
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        device_scale_factor=1,
    )


async def stop_browser():
    global playwright, browser, context
    try:
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
    except Exception:
        pass
    playwright = browser = context = None


async def _load_html() -> str:
    page = await context.new_page()
    try:
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_function(
            "() => typeof DisconSchedule !== 'undefined' && !!DisconSchedule.fact",
            timeout=12000,
        )
        return await page.content()
    finally:
        await page.close()


async def fetch_fact():
    await start_browser()
    try:
        text = await _load_html()
        fact = extract_fact_from_html(text)
        if fact and "data" in fact:
            return fact, extract_attention_from_html(text)
        raise RuntimeError("DisconSchedule.fact not found in page content")
    except Exception as e:
        logging.error("Fetch failed: %s", e)
        await stop_browser()
        raise e


def _cache_fresh() -> bool:
    return bool(_cache["snap"]) and time.monotonic() - _cache["ts"] < CACHE_TTL


async def get_snapshot(force=False):
    async with fetch_lock:
        if not force and _cache_fresh():
            return _cache["snap"]

        fact, attention = await fetch_fact()
        schedules = parse_schedules(fact)
        if not schedules:
            raise RuntimeError(f"Group {GROUP} not found in fact data")

        snap = {
            "schedules": schedules,
            "updated": fact.get("update", "---"),
            "attention": attention,
        }
        _cache["ts"] = time.monotonic()
        _cache["snap"] = snap
        return snap


# ============================= TIME CALC =============================
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


def render_message(snap: dict, header: str) -> str:
    now = now_kyiv()
    by_date = {d["date"]: d for d in snap["schedules"].values()}
    today_date = now.date()
    tomorrow_date = today_date + timedelta(days=1)

    today = by_date.get(today_date)
    tomorrow = by_date.get(tomorrow_date)

    blocks = []
    
    # Блок на сегодня
    if today:
        blocks.append(f"⚡{day_title(today['date'], now)}⚡\n{today['text']}")
    else:
        blocks.append(f"⚡на сегодня {today_date.strftime('%d.%m.%Y')}⚡\n⚠️ График на сегодня отсутствует, нужно подождать пока появится")

    # Блок на завтра
    if tomorrow:
        blocks.append(f"⚡{day_title(tomorrow['date'], now)}⚡\n{tomorrow['text']}")
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
                    if old is not None and old != d["sig"]:
                        changed = True
                    await rcall(redis_client.set, key, d["sig"], ex=172800)

                if changed:
                    logging.info("📢 Schedule changed, notifying users")
                    msg = render_message(snap, "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ</b>")
                    builder = InlineKeyboardBuilder()
                    builder.row(types.InlineKeyboardButton(
                        text="🔹 Информация на сегодня", callback_data="show_att"))

                    for uid in users:
                        try:
                            await bot.send_message(int(_s(uid)), msg,
                                                   reply_markup=builder.as_markup(),
                                                   parse_mode="HTML")
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logging.error("Broadcast to %s failed: %s", uid, e)
        except Exception as e:
            logging.error("⚠️ Monitoring error: %s", e)
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

    # Мгновенная выдача из кэша без ожидания парсинга сайта
    snap = _cache["snap"]
    if not snap:
        try:
            snap = await get_snapshot()
        except Exception:
            await m.answer("❌ <b>Ошибка:</b> график временно недоступен, попробуйте через минуту.", parse_mode="HTML")
            return

    await m.answer(render_message(snap, "💡<b>ГРАФИК ОТКЛЮЧЕНИЙ</b>"),
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
        asyncio.create_task(start_web_server())  # Запуск веб-сервера для UptimeRobot
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in background:
            t.cancel()
        await stop_browser()


if __name__ == "__main__":
    asyncio.run(main())
