import asyncio
import logging
import json
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from upstash_redis import Redis
from playwright.async_api import async_playwright

# --- КОНФИГ ---
TOKEN = "8702758834:AAHbQNtVyNl85z2xtPiuHlAbUfPSBqtCshA"
REDIS_URL = "https://driven-fox-52037.upstash.io"
REDIS_TOKEN = "ActFAAIncDI4YzQwMjBhNzkxNzY0YmYzYjFhN2FmZGJkODg0NmFiMHAyNTIwMzc"

CITY, STREET, HOUSE = "с. Мала Михайлівка", "вул. Бесарабська", "32/"

bot = Bot(token=TOKEN)
dp = Dispatcher()
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

logging.basicConfig(level=logging.INFO)

# Переменные браузера
playwright = None
browser = None
# Две отдельные страницы и два замка
page_monitor = None
page_user = None
lock_monitor = asyncio.Lock()
lock_user = asyncio.Lock()

# ТРЕКЕР ОБНОВЛЕНИЯ ДЛЯ МОНИТОРИНГА
last_monitor_reload = None

# =============================
# 🔥 JS анализ графика
# =============================
analysis_script = """
() => {
    const activeTab = document.querySelector("#discon-fact .dates .date.active");
    const dateId = activeTab ? activeTab.getAttribute("rel") : null;
    const dateTextElem = activeTab ? activeTab.querySelector("div:nth-child(2)") : null;
    const dateText = dateTextElem ? dateTextElem.innerText.trim() : "Графік";
    const updateTimeElem = document.querySelector("#discon-fact .discon-fact-info-text");
    const updateTime = updateTimeElem ? updateTimeElem.innerText.trim() : "---";
    const row = document.querySelector("#discon-fact .discon-fact-table.active table tbody tr");
    if (!row) return { dateId, dateText, schedule: "Графік не знайдено", raw_statuses: [], updateTime };
    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
    let raw_statuses = [];
    cells.forEach(c => {
        let s1 = (c.classList.contains('cell-scheduled') || c.classList.contains('cell-first-half')) ? "🔴" : "🟢";
        let s2 = (c.classList.contains('cell-scheduled') || c.classList.contains('cell-second-half')) ? "🔴" : "🟢";
        raw_statuses.push(s1, s2);
    });
    let intervals = [];
    const fmt = (idx) => {
        let m = idx * 30;
        return String(Math.floor(m/60)).padStart(2,'0') + ":" + String(m%60).padStart(2,'0');
    };
    let cur = raw_statuses[0], start = 0;
    for (let i = 1; i <= 48; i++) {
        if (i === 48 || raw_statuses[i] !== cur) {
            intervals.push(cur + " <b>" + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + "</b>" + cur);
            if(i < 48) { cur = raw_statuses[i]; start = i; }
        }
    }
    return { dateId, dateText, schedule: intervals.join("\\n"), raw_statuses, updateTime };
}
"""

# =============================
# 🌐 Логика браузера
# =============================
async def setup_page(ctx):
    p = await ctx.new_page()

    # Блокируем ВСЁ кроме document + xhr + fetch
    await p.route("**/*", lambda route: route.abort()
        if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"] 
        else route.continue_()
    )

    # Убираем анимации (ускоряет автокомплит)
    await p.add_init_script("""
        const style = document.createElement('style');
        style.innerHTML = `* { transition: none !important; animation: none !important; }`;
        document.head.appendChild(style);
    """)

    return p

async def start_browser():
    global playwright, browser, page_monitor, page_user
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"])

    context = await browser.new_context(user_agent="Mozilla/5.0")
    
    page_monitor = await setup_page(context)
    page_user = await setup_page(context)
    
    # Первичная загрузка
    await reload_page(page_monitor)
    await reload_page(page_user)

async def reload_page(p):
    logging.info(f"⚡ Перезагрузка и ввод адреса на странице...")
    try:
        await p.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="domcontentloaded", timeout=30000)
        try: await p.click("button.modal__close", timeout=500)
        except: pass

        for sel, val, lid in [("input[name='city']", CITY, "city"), ("input[name='street']", STREET, "street"), ("input#house_num", HOUSE, "house_num")]:
            field = p.locator(sel).first
            await field.wait_for(state="attached", timeout=5000)
            await field.fill(val)
            try:
                item = p.locator(f"#{lid}autocomplete-list div").first
                await item.wait_for(state="attached", timeout=2000)
                await item.click()
            except:
                await p.keyboard.press("ArrowDown")
                await p.keyboard.press("Enter")
        await p.wait_for_selector("#discon-fact", timeout=10000)
    except Exception as e:
        logging.error(f"❌ Ошибка перезагрузки: {e}")

# =============================
# 📊 Универсальный парсер
# =============================
async def fetch_data(p, lock, force=False):
    async with lock:
        if force:
            await reload_page(p)
        
        try:
            result = {}
            tabs = p.locator("#discon-fact .dates .date")
            count = await tabs.count()
            if count == 0: 
                await reload_page(p)
                return await fetch_data(p, lock, force=False)

            for i in range(count):
                tab = tabs.nth(i)
                await tab.click(timeout=5000)
                data = await p.evaluate(analysis_script)
                if data and data.get("dateId"):
                    result[data["dateId"]] = data
            return result
        except:
            return {}
        
def h_str(h):
        return str(int(h)) if h % 1 == 0 else str(h)

# =============================
# ⏳ Расчет времени (Остается без изменений)
# =============================
def calculate_time_left(schedules):
    if not schedules:
        return "Нет данных для расчета."

    tz = pytz.timezone('Europe/Kiev')
    now = datetime.now(tz)
    
    sorted_rels = sorted(schedules.keys())
    today_rel = sorted_rels[0]
    
    raw_today = schedules[today_rel].get('raw_statuses', [])
    if not raw_today:
        return "График на сегодня пуст."
    off_intervals = raw_today.count("🔴")
    on_intervals = raw_today.count("🟢")
    
    # Переводим в часы (каждый интервал = 0.5 часа)
    hours_off = off_intervals / 2
    hours_on = on_intervals / 2
    
    # Красивое форматирование: убираем .0 если число целое

    # Берем завтрашний день для переходов через полночь
    raw_tomorrow = []
    if len(sorted_rels) > 1:
        raw_tomorrow = schedules[sorted_rels[1]].get('raw_statuses', [])

    full_timeline = raw_today + raw_tomorrow
    
    minutes_now = now.hour * 60 + now.minute
    current_idx = minutes_now // 30
    
    if current_idx >= len(raw_today):
        return "Сегодняшний график уже не актуален."

    current_state = full_timeline[current_idx]
    
    # --- 1. Ищем ПЕРВОЕ изменение (ближайшее) ---
    first_change_idx = -1
    for i in range(current_idx + 1, len(full_timeline)):
        if full_timeline[i] != current_state:
            first_change_idx = i
            break
            
    if first_change_idx == -1:
        return f"<blockquote>✨Сегодня без отключений✨</blockquote>\n"

    # Расчет времени до 1-го события
    diff1 = (first_change_idx * 30) - minutes_now
    h1, m1 = diff1 // 60, diff1 % 60
    action1 = "<b>Включение через:</b>" if current_state == "🔴" else "<b>Выключение через:</b>"
    
    res = f"<blockquote>💡<b>Статус:</b>{current_state}💡</blockquote>\n<b><i>⏳</i>{action1}</b> <b>{h1}</b><b>ч</b> <b>{m1}</b> <b>минут</b><b>.</b>"
    # --- 2. Ищем ВТОРОЕ изменение (следующее за первым) ---
    second_change_idx = -1
    next_state = full_timeline[first_change_idx]
    for i in range(first_change_idx + 1, len(full_timeline)):
        if full_timeline[i] != next_state:
            second_change_idx = i
            break
    
    if second_change_idx != -1:
        # Расчет времени от ТЕКУЩЕГО момента до 2-го события
        diff2 = (second_change_idx * 30) - minutes_now
        h2, m2 = diff2 // 60, diff2 % 60
        action2 = "<b>Включение через:</b>" if next_state == "🔴" else "<b>Выключение через:</b>"
        
        # Добавляем инфо про второе событие
        res += f"\n<b>⏳{action2}</b> <b>{h2}</b><b>ч</b> <b>{m2}</b> <b>минут</b><b>.</b>\n"
        res += f"📊 <b>За сегодня: 🟢 {h_str(hours_on)}ч, 🔴 {h_str(hours_off)}ч</b>\n"

    return res
# =============================
# 📡 Мониторинг (КД 60 сек) - ИСПРАВЛЕННЫЙ
# =============================
async def monitoring_task():
    global last_monitor_reload
    while True:
        try:
            await asyncio.sleep(300) 
            users = redis.smembers("monitoring_users")
            if not users: continue

            now = datetime.now()
            should_reload = False
            # Интервал проверки сайта — 2 минуты
            if last_monitor_reload is None or (now - last_monitor_reload) > timedelta(seconds=120):
                should_reload = True
                last_monitor_reload = now

            schedules = await fetch_data(page_monitor, lock_monitor, force=should_reload)
            if not schedules: continue

            for uid_bytes in users:
                uid = uid_bytes.decode() if isinstance(uid_bytes, bytes) else uid_bytes
                has_real_change = False
                
                for rel, data in schedules.items():
                    cache_key = f"sched:{uid}:{rel}"
                    cached = redis.get(cache_key)
                    cached_str = cached.decode() if isinstance(cached, bytes) else cached

                    
                    # Если этот день УЖЕ БЫЛ в кэше и график СТАЛ другим — это реальное изменение
                    if cached_str is not None and cached_str != data["schedule"]:
                        has_real_change = True
                    
                    # Обновляем кэш для этой даты (на 2 дня)
                    redis.set(cache_key, data["schedule"], ex=172800)

                # Отправляем сообщение только если зафиксировано изменение в существующих датах
                if has_real_change:
                    ans = calculate_time_left(schedules)
                    
                    # Шапка уведомления
                    msg = "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!!</b>\n"
                    msg += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"

                    # Графики (делаем моноширинными через <code>)
                    for r in sorted(schedules.keys()):
                        msg += f"⚡<b>{schedules[r]['dateText']}</b>⚡\n"
                        msg += f"<code>{schedules[r]['schedule']}</code>\n"
                    
                    msg += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                    
                    # Техническая инфа (время обновления)
                    raw_time = list(schedules.values())[0]['updateTime']
                    clean_time = raw_time.split(": ")[-1] if ": " in raw_time else raw_time
                    msg += ans                    
                    msg += f"🕒 <b>Обновлено:</b> <code>{clean_time}</code>\n"
                    try:
                        await bot.send_message(int(uid), msg, parse_mode="HTML")
                        logging.info(f"✅ Отправлено уведомление юзеру {uid}")
                    except Exception as e:
                        logging.error(f" Ошибка отправки: {e}")

        except Exception as e:
            logging.error(f"⚠️ Ошибка в мониторинге: {e}")
            await asyncio.sleep(30)
# =============================
# 🤖 Обработка юзера
# =============================
@dp.message(F.text.contains("график") | F.text.contains("Показать"))
async def manual(m: types.Message):
    msg = await m.answer("🔍 Проверяю сайт (полное обновление)...")
    # Для юзера ВСЕГДА force=True
    schedules = await fetch_data(page_user, lock_user, force=True)
    
    if not schedules:
        await msg.edit_text("❌ Не удалось получить данные.")
        return

    ans = calculate_time_left(schedules)   
    
    # Заголовок
    full_text = "💡<b>Актуальный График</b>💡\n"
    full_text += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    
    # Графики
    for rel in sorted(schedules.keys()):
        d = schedules[rel]
        full_text += f"⚡<b>{d['dateText']}</b>⚡\n"
        full_text += f"<code>{d['schedule']}</code>\n"

    full_text += "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    
    # Время обновления
    raw_time = list(schedules.values())[0]['updateTime']
    clean_time = raw_time.split(": ")[-1] if ": " in raw_time else raw_time
    
    full_text += ans
    full_text += f"🕒 <b>Обновлено:</b> <code>{clean_time}</code>\n"
    
    await msg.edit_text(full_text, parse_mode="HTML")
def get_kb(uid):
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Показать график 💡")], [types.KeyboardButton(text="Вкл/Выкл мониторинг 📡")]], 
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот запущен.", reply_markup=get_kb(m.from_user.id))

@dp.message(F.text.contains("мониторинг"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("Мониторинг включен.")

async def main():   
    await start_browser()
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
