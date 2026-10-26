"""
DTEK Kyiv-region outage bot - GROUP-ONLY, speed-optimised.
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
import redis  # Используем стандартный redis-py
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright

# ============================= CONFIG =============================
TOKEN = "8699245998:AAFrajfnUyi7lVNdPvzrykAz6KjWcgAtXHw"

# Подключение к Redis по твоему хосту и порту
REDIS_HOST = "wrench-microstrong-back-61275.db.redis.io"
REDIS_PORT = 17778
REDIS_PASSWORD = "ZZjNkOsY6RK6iojvs721XY8sYB8c4fHM"

GROUP = "GPV4.1"                    # <- your queue/group
SITE_URL = "https://www.dtek-krem.com.ua/ua/shutdowns"
SITE_HOST = "dtek-krem.com.ua"
HEADLESS = os.getenv("HEADLESS", "0") == "1"
CHECK_INTERVAL = 300                  # monitoring period, seconds
CACHE_TTL = 30                        # manual requests reuse data younger than this
FAST_RETRY_AFTER = 3600               # after a fast-mode failure, wait this long before trying it again
DEFAULT_ATTENTION = "Актуальних повідомлень немає."
TZ = pytz.timezone("Europe/Kiev")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация клиента Redis
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
page = None
fetch_lock = asyncio.Lock()
_block_assets = True                  # read by the route handler
_fast_disabled_until = 0.0

# last successfully parsed snapshot
_cache = {"ts": 0.0, "snap": None}


def now_kyiv():
    return datetime.now(TZ)


def _s(x):
    """Redis may return bytes or str depending on client settings."""
    return x.decode() if isinstance(x, bytes) else x


async def rcall(fn, *args, **kwargs):
    """Run a blocking Redis call in a thread so it never stalls the bot."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ============================= PARSING =============================
def extract_fact_from_html(page_html: str):
    """Pull `DisconSchedule.fact = {...}` out of raw HTML."""
    m = re.search(r"DisconSchedule\.fact\s*=\s*", page_html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(page_html[m.end():])
        return obj
    except ValueError:
        return None


def extract_attention_from_html(page_html: str) -> str:
    """Text of the 'Шановні клієнти!' notice (.m-attention__text), if the site shows one."""
    m = re.search(r'class="[^"]*m-attention__text[^"]*"[^>]*>(.*?)</div>', page_html, re.S)
    if not m:
        return DEFAULT_ATTENTION
    text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)   # "мережі ." -> "мережі."
    return text or DEFAULT_ATTENTION


def day_to_slots(day: dict) -> list:
    """24 hourly values -> 48 half-hour slots of 🟢 / 🔴."""
    slots = []
    for h in range(1, 25):
        v = day.get(str(h), "yes")
        slots.append("🔴" if v in ("no", "first") else "🟢")
        slots.append("🔴" if v in ("no", "second") else "🟢")
    return slots


def slots_to_text(slots: list) -> str:
    """Only the OUTAGE intervals - power-on periods are not listed."""
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
# ============================= BROWSER =============================
async def _route_handler(route):
    try:
        req = route.request
        is_site_doc = req.resource_type == "document" and SITE_HOST in req.url
        if _block_assets and not is_site_doc:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


async def start_browser():
    global playwright, browser, page
    playwright = await async_playwright().start()
    # ДОБАВЛЕНЫ КРИТИЧЕСКИЕ ФЛАГИ ДЛЯ EKONOMИИ ПАМЯТИ В LINUX/DOCKER
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--single-process" # помогает экономить RAM на слабых серверах
        ]
    )
    context = await browser.new_context(user_agent="Mozilla/5.0")
    await context.route("**/*", _route_handler)
    page = await context.new_page()


async def stop_browser():
    global playwright, browser, page
    try:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
    except Exception:
        pass
    playwright = browser = page = None


async def fetch_fact():
    last_err = RuntimeError("DisconSchedule.fact not found on the page")

    try:
        if page is None:
            await start_browser()
        
        # Сразу делаем полноценную загрузку (без блокировки ассетов)
        text = await _load_html(fast=False)
        fact = extract_fact_from_html(text)
        
        if fact and "data" in fact:
            return fact, extract_attention_from_html(text)
            
        logging.warning("DisconSchedule.fact not in response")
    except Exception as e:
        last_err = e
        logging.error("Fetch failed: %s", e)
        await stop_browser()   # перезапуск браузера при ошибке
        
    raise last_err

async def _load_html(fast: bool) -> str:
    global _block_assets
    _block_assets = fast
    if fast:
        resp = await page.goto(SITE_URL, wait_until="commit", timeout=20000)
        return await resp.text() if resp else await page.content()

    await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_function(
            "() => typeof DisconSchedule !== 'undefined' && !!DisconSchedule.fact",
            timeout=15000,
        )
    except Exception as e:
        logging.warning("Full load: DisconSchedule.fact did not appear (%s)", e)
    return await page.content()



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
    current = timeline[idx]

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
    blocks = "\n\n".join(
        f"⚡{day_title(d['date'], now)}⚡\n{d['text']}" for d in snap["schedules"].values()
    )
    return (
        f"{header}\n"
        f"<pre>{blocks}</pre>\n"
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

    if _cache_fresh():
        await m.answer(render_message(_cache["snap"], "💡<b>ГРАФИК ОТКЛЮЧЕНИЙ</b>"),
                       reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    msg = await m.answer("⏳ Загрузка...")
    try:
        snap = await get_snapshot()
    except Exception:
        logging.exception("Manual fetch failed")
        await msg.edit_text("❌ <b>Ошибка:</b> не удалось получить данные с сайта.",
                            parse_mode="HTML")
        return
    await msg.edit_text(render_message(snap, "💡<b>ГРАФИК ОТКЛЮЧЕНИЙ</b>"),
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
        asyncio.create_task(monitoring_task())
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in background:
            t.cancel()
        await stop_browser()


if __name__ == "__main__":
    asyncio.run(main())
