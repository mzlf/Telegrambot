import time
import telebot
import threading
from playwright.sync_api import sync_playwright

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8702758834:AAHbQNtVyNl85z2xtPiuHlAbUfPSBqtCshA"
bot = telebot.TeleBot(TOKEN)

# Глобальные объекты
active_users = set()       # Кто в процессе парсинга сейчас
last_request_time = {}     # Время последнего запроса для КД (user_id: timestamp)
users_lock = threading.Lock()
browser_lock = threading.Lock() # Строго по очереди для стабильности на сайте

def get_dtek_analysis(day_type="today"):
    """Запуск браузера и парсинг"""
    with browser_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            try:
                page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="networkidle", timeout=60000)
                try: page.click("button.modal__close", timeout=5000)
                except: pass

                def safe_fill(p, selector, value, list_id):
                    f = p.locator(selector).first
                    f.wait_for(state="visible", timeout=15000)
                    f.scroll_into_view_if_needed()
                    f.click(force=True)
                    p.keyboard.press("Control+A")
                    p.keyboard.press("Backspace")
                    f.type(value)
                    p.keyboard.press("ArrowDown")
                    s = f"#{list_id}autocomplete-list div, .autocomplete-suggestion:visible"
                    p.wait_for_selector(s, state="visible", timeout=15000)
                    p.locator(s).first.click(force=True)

                safe_fill(page, "input[name='city']", "с. Мала Михайлівка", "city")
                safe_fill(page, "input[name='street']", "вул. Бесарабська", "street")
                safe_fill(page, "input#house_num, input[name='house']", "32/", "house_num")

                table_path = "#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table"
                page.wait_for_selector(table_path, timeout=20000)
                
                if day_type == "tomorrow":
                    tab = page.locator("#discon-fact > div.dates > div:nth-child(2)")
                    if not tab.is_visible(): return "График на завтра еще не опубликован."
                    tab.click(force=True)
                    time.sleep(2)

                analysis_script = """
                () => {
                    const row = document.querySelector("#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table > tbody > tr");
                    if (!row) return "График не найден.";
                    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
                    let intervals = [];
                    cells.forEach((cell, index) => {
                        let hour = index;
                        if (cell.classList.contains('cell-scheduled')) intervals.push({start: hour, end: hour + 1});
                        else if (cell.classList.contains('cell-first-half')) intervals.push({start: hour, end: hour + 0.5});
                        else if (cell.classList.contains('cell-second-half')) intervals.push({start: hour + 0.5, end: hour + 1});
                    });
                    if (intervals.length === 0) return "✅ Свет отключать не планируют.";
                    let merged = [];
                    let current = intervals[0];
                    for (let i = 1; i < intervals.length; i++) {
                        if (intervals[i].start === current.end) current.end = intervals[i].end;
                        else { merged.push(current); current = intervals[i]; }
                    }
                    merged.push(current);
                    const fmt = (t) => {
                        let h = Math.floor(t).toString().padStart(2, '0');
                        let m = (t % 1) === 0 ? "00" : "30";
                        return h + ":" + m;
                    };
                    return merged.map(i => "🔴 <b>" + fmt(i.start) + " — " + fmt(i.end) + "</b>").join('\\n');
                }
                """
                result = page.evaluate(analysis_script)
                browser.close()
                return result
            except Exception as e:
                browser.close()
                return f"Ошибка: {str(e)}"

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    uid = message.from_user.id
    current_time = time.time()
    text = message.text.lower() # Переводим в нижний регистр для удобства

    # 1. Проверяем, что текст сообщения нам подходит
    if "сьогодні" in text or "сегодня" in text or "💡" in text:
        day = "today"
    elif "завтра" in text or "📅" in text:
        day = "tomorrow"
    else:
        # Если юзер написал что-то другое
        bot.reply_to(message, "❓ Я тебя не понимаю. Нажми на кнопку в меню или напиши 'Сегодня'/'Завтра'.")
        return # Выходим из функции, браузер не запустится
        # 1. ПРОВЕРКА КД (10 секунд)
    if uid in last_request_time:
        elapsed = current_time - last_request_time[uid]
        if elapsed < 10:
            remaining = int(10 - elapsed)
            bot.reply_to(message, f"⚠️ Не спеши! Подожди еще {remaining} сек.")
            return

    # 2. ПРОВЕРКА АКТИВНОГО ПРОЦЕССА
    with users_lock:
        if uid in active_users:
            bot.reply_to(message, "⏳ Твой запрос уже обрабатывается!")
            return
        active_users.add(uid)

    # Обновляем время последнего запроса
    last_request_time[uid] = current_time

    def task():
        try:
            day = "tomorrow" if "Завтра" in message.text else "today"
            status = bot.send_message(message.chat.id, f"🔍 Запрашиваю данные (в очереди)...")
            
            result_text = get_dtek_analysis(day)
            final_message = f"<b>📢 График на {message.text.lower()}:</b>\n\n{result_text}"
            
            bot.edit_message_text(final_message, message.chat.id, status.message_id, parse_mode="HTML")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            with users_lock:
                if uid in active_users:
                    active_users.remove(uid)

    threading.Thread(target=task).start()

if __name__ == "__main__":
    bot.polling(none_stop=True)
