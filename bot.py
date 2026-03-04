import asyncio
import logging
import json
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
page_monitor = None
page_user = None
current_attention_text = "Актуальних повідомлень немає."
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
    
    // ПАРСИНГ СИНЕГО БЛОКА ВНИМАНИЯ
    const attentionElem = document.querySelector(".m-attention__text");
    const attentionText = attentionElem ? attentionElem.innerText.trim() : "Актуальних повідомлень від Укренерго немає.";

    const updateTimeElem = document.querySelector("#discon-fact .discon-fact-info-text");
    const updateTime = updateTimeElem ? updateTimeElem.innerText.trim() : "---";
    
    const row = document.querySelector("#discon-fact .discon-fact-table.active table tbody tr");
    if (!row) return { dateId, dateText, schedule: "Графік не знайдено", raw_statuses: [], updateTime, attention: attentionText };
    
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
            intervals.push(cur + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + cur);
            if(i < 48) { cur = raw_statuses[i]; start = i; }
        }
    }
    return { dateId, dateText, schedule: intervals.join("\\n"), raw_statuses, updateTime, attention: attentionText };
}
"""
clock_frames = ["◐","◓","◑","◒"]

class ProgressSpinner:
    def __init__(self, message):
        self.message = message
        self.running = True
        self.stage_text = "Запуск..."
        self.task = None
        self.frame = 0

    async def start(self):
        self.task = asyncio.create_task(self._spin())

    async def _spin(self):
        while self.running:
            text = f"{clock_frames[self.frame % len(clock_frames)]} {self.stage_text} "
            try:
                await self.message.edit_text(text)
            except:
                pass
            self.frame += 1
            await asyncio.sleep(0.5)

    async def update(self, new_stage):
        self.stage_text = new_stage

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

async def delete_message_after(message: types.Message, sleep_time: int):
    await asyncio.sleep(sleep_time)
    try:
        await message.delete()
    except Exception:
        pass
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
async def fetch_data(p, lock, force=False, progress=None):
    async with lock:

        if progress:
            await progress.update("Открываю сайт...")
        if force:
            await reload_page(p)

        if progress:
            await progress.update("Получаю вкладки...")
        tabs = p.locator("#discon-fact .dates .date")
        count = await tabs.count()

        if count == 0:
            if progress:
                await progress.update("Перезагрузка...")
            await reload_page(p)
            return await fetch_data(p, lock, False, progress)

        result = {}

        for i in range(count):
            if progress:
                await progress.update(f"Обрабатываю день {i+1}/{count}...")
            tab = tabs.nth(i)
            await tab.click(timeout=5000)
            data = await p.evaluate(analysis_script)

            if data and data.get("dateId"):
                result[data["dateId"]] = data

        if progress:
            await progress.update("Анализирую данные...")
        return result        

# =============================
# ⏳ Расчет времени
# =============================
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

    # Собираем общую линию времени (сегодня + завтра если есть)
    raw_tomorrow = []
    if len(sorted_rels) > 1:
        raw_tomorrow = schedules[sorted_rels[1]].get('raw_statuses', [])
    
    full_timeline = raw_today + raw_tomorrow
    minutes_now = now.hour * 60 + now.minute
    current_idx = minutes_now // 30
    if current_idx >= len(raw_today):
        return "Сегодняшний график уже не актуален."

    current_state = full_timeline[current_idx]
    
    # --- Логика поиска изменений ---
    events = []
    temp_state = current_state
    
    # Ищем два ближайших изменения статуса
    for i in range(current_idx + 1, len(full_timeline)):
        if full_timeline[i] != temp_state:
            diff = (i * 30) - minutes_now
            h, m = diff // 60, diff % 60
            
            label = "<b>До включения</b>" if full_timeline[i] == "🟢" else "<b>До выключения</b>"
            events.append(f" ↳ {label}: {format_time(h, m)}")
            
            temp_state = full_timeline[i]
            if len(events) >= 2: # Нам нужно только два ближайших события
                break

    # --- Сборка сообщения ---
    status_text = "🔋 Включено" if current_state == "🟢" else "🪫 Отключено"
    
    res = f"💡 <b>Текущий статус</b>\n"
    res += f" ↳ <b>Сейчас</b>: {status_text}\n"
    
    # Добавляем найденные события
    if events:
        res += "\n".join(events) + "\n"
    else:
        res += " ↳ <b>✨ Отключений не планируется</b>\n"

    # --- Статистика ---
    today_off = raw_today.count("🔴") / 2
    today_on = raw_today.count("🟢") / 2
    
    res += f"📊 <b>Статистика</b>\n"
    res += f" ↳ <b>Сегодня</b>: 🔋 {h_str(today_on)} ч | 🪫 {h_str(today_off)} ч\n"

    if raw_tomorrow:
        tomorrow_off = raw_tomorrow.count("🔴") / 2
        tomorrow_on = raw_tomorrow.count("🟢") / 2
        res += f" ↳ <b>Завтра</b>: 🔋 {h_str(tomorrow_on)} ч | 🪫 {h_str(tomorrow_off)} ч\n"

    return res
# =============================
# 📡 Мониторинг (КД 60 сек) - ИСПРАВЛЕННЫЙ
# =============================
async def monitoring_task():
    global last_monitor_reload, current_attention_text
    while True:
        try:
            await asyncio.sleep(300) # Проверка каждые 5 минут
            users = redis.smembers("monitoring_users")
            if not users: continue

            now = datetime.now()
            should_reload = (last_monitor_reload is None or 
                           (now - last_monitor_reload) > timedelta(seconds=120))
            
            if should_reload:
                last_monitor_reload = now

            schedules = await fetch_data(page_monitor, lock_monitor, force=should_reload)
            if not schedules: continue

            # Проверяем, изменился ли график ХОТЯ БЫ для одного дня в глобальном кэше
            has_real_change = False
            for rel, data in schedules.items():
                cache_key = f"global_sched:{rel}" # Общий ключ для всех
                cached = redis.get(cache_key)
                cached_str = cached.decode() if isinstance(cached, bytes) else cached

                if cached_str is not None and cached_str != data["schedule"]:
                    has_real_change = True
                
                # Обновляем глобальный кэш
                redis.set(cache_key, data["schedule"], ex=172800)

            # Обновляем общий текст внимания
            first_day_rel = sorted(schedules.keys())[0]
            current_attention_text = schedules[first_day_rel].get('attention', "Інформація відсутня.")

            if has_real_change:
                logging.info("📢 Зафиксировано изменение графика! Рассылка...")
                
                # Формируем ОДНО сообщение для всех
                ans = calculate_time_left(schedules)
                unified_schedule = ""
                for rel in sorted(schedules.keys()):
                    d = schedules[rel]
                    unified_schedule += f"⚡{d['dateText']} ⚡\n{d['schedule']}\n\n"

                raw_time = schedules[first_day_rel]['updateTime']
                clean_time = raw_time.split(": ")[-1] if ": " in raw_time else raw_time
                
                msg = (
                    "🔔 <b>ГРАФИК ИЗМЕНИЛСЯ!!</b>\n"
                    f"<pre>{unified_schedule.strip()}</pre>\n"
                    f"{ans}\n"
                    f"<pre>🕒 Обновлено: {clean_time}</pre>"
                )

                # Кнопка для всех
                builder = InlineKeyboardBuilder()
                builder.row(types.InlineKeyboardButton(text="🔹 Информация на сегодня", callback_data="show_att"))

                # Рассылка по списку юзеров
                for uid_bytes in users:
                    uid = uid_bytes.decode() if isinstance(uid_bytes, bytes) else uid_bytes
                    try:
                        await bot.send_message(int(uid), msg, reply_markup=builder.as_markup(), parse_mode="HTML")
                        await asyncio.sleep(0.05) # Защита от спам-фильтра Telegram
                    except Exception as e:
                        logging.error(f"Ошибка рассылки юзеру {uid}: {e}")

        except Exception as e:
            logging.error(f"⚠️ Ошибка в мониторинге: {e}")
            await asyncio.sleep(30)
# =============================
# 🤖 Обработка юзера
# =============================
@dp.message(F.text.contains("график") | F.text.contains("Показать"))
async def manual(m: types.Message):
    msg = await m.answer("◐ Запуск...")
    spinner = ProgressSpinner(msg)
    await spinner.start()

    try:
        schedules = await fetch_data(page_user, lock_user, force=True, progress=spinner)
        if not schedules:
            await spinner.stop()
            await msg.edit_text("❌ Ошибка данных.")
            return

        # Собираем все графики в один блок <pre>
        unified_schedule = ""
        sorted_keys = sorted(schedules.keys())
        for rel in sorted_keys:
            d = schedules[rel]
            unified_schedule += f"⚡{d['dateText']} ⚡\n{d['schedule']}\n\n"

        # Сохраняем текст уведомления для кнопки
        global current_attention_text
        current_attention_text = schedules[sorted_keys[0]].get('attention', "Нету информации.")
        ans = calculate_time_left(schedules)
        raw_time = schedules[sorted_keys[0]]['updateTime']
        clean_time = raw_time.split(": ")[-1] if ": " in raw_time else raw_time

        full_text = f"💡<b>Актуальний Графік</b>💡\n"
        full_text += f"<pre>{unified_schedule.strip()}</pre>\n"
        full_text += ans
        full_text += f"<pre>🕒 <b>Обновлено:</b> {clean_time}</pre>"

        # Создаем кнопку
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="🔹 Информация на сегодня", callback_data="show_att"))

        await spinner.stop()
        await msg.edit_text(full_text, reply_markup=builder.as_markup(), parse_mode="HTML")

    except Exception as e:
        await spinner.stop()
        logging.error(e)
        await msg.edit_text("❌ Произошла ошибка.")

@dp.callback_query(F.data == "show_att")
async def callback_att(call: types.CallbackQuery):
    # Отправляем сообщение и сохраняем объект сообщения в переменную 'info_msg'
    info_msg = await call.message.answer(
        f"📢 <b>Повідомлення Укренерго:</b>\n\n{current_attention_text}", 
        parse_mode="HTML"
    )
    await call.answer() # Убираем "часики" с кнопки

    # Запускаем фоновую задачу удаления, чтобы не блокировать бота
    asyncio.create_task(delete_message_after(info_msg, 120)) # 120 секунд (2 минуты)

def get_kb():
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="⚡ Показать график")], [types.KeyboardButton(text="🔔 Мониторинг")]], 
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def start_cmd(m: types.Message):
    await m.answer("Бот запущен.", reply_markup=get_kb())

@dp.message(F.text.contains("мониторинг") | F.text.contains("Мониторинг") | F.text.contains("🔔"))
async def toggle(m: types.Message):
    uid = str(m.from_user.id)
    if redis.sismember("monitoring_users", uid):
        redis.srem("monitoring_users", uid)
        await m.answer("Мониторинг выключен.")
    else:
        redis.sadd("monitoring_users", uid)
        await m.answer("Мониторинг включен.")
@dp.message(F.text)
async def default_handler(m: types.Message):
    kb = [
        [types.KeyboardButton(text="⚡ Показать график")],
        [types.KeyboardButton(text="🔔 Мониторинг")]
    ]
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие в меню 👇"
    )
    
    await m.answer("Пожалуйста, выберите действие из меню ниже:", reply_markup=keyboard)

async def main():   
    await start_browser()
    asyncio.create_task(monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
