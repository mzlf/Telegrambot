import os
import time
import telebot
import threading
import re
from playwright.sync_api import sync_playwright

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8702758834:AAHbQNtVyNl85z2xtPiuHlAbUfPSBqtCshA"
# Данные лучше вынести в переменные Railway (как мы делали раньше)
CITY = "с. Мала Михайлівка"
STREET = "вул. Бесарабська"
HOUSE = "32/"

bot = telebot.TeleBot(TOKEN)

# Глобальные объекты
monitoring_users = set()   
last_known_today = {}      # Храним только текст графика (эмодзи)
last_known_tomorrow = {}
active_users = set()
users_lock = threading.Lock()
browser_lock = threading.Lock()

def get_dtek_full_data():
    """Заходит один раз и берет данные за сегодня и завтра"""
    with browser_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()
            # Оставляем CSS для работы выпадающих списков
            page.route("**/*.{png,jpg,jpeg,svg,woff,woff2}", lambda route: route.abort())

            try:
                page.goto("https://www.dtek-krem.com.ua/ua/shutdowns", wait_until="networkidle", timeout=45000)
                try: page.click("button.modal__close", timeout=5000)
                except: pass

                def safe_fill(p, selector, value, list_id):
                    f = p.locator(selector).first
                    f.wait_for(state="visible", timeout=15000)
                    f.click()
                    p.keyboard.press("Control+A")
                    p.keyboard.press("Backspace")
                    f.fill(value) # Печатаем медленно
                    
                    s = f"#{list_id}autocomplete-list div, .autocomplete-suggestion:visible"
                    try:
                        p.wait_for_selector(s, state="visible", timeout=10000)
                        p.locator(s).first.click(force=True)
                    except:
                        # Если список не вывалился, пробуем выбрать "вслепую"
                        p.keyboard.press("ArrowDown")
                        p.keyboard.press("Enter")
                        print("cant find dropdown")

                safe_fill(page, "input[name='city']", CITY, "city")
                safe_fill(page, "input[name='street']", STREET, "street")
                safe_fill(page, "input#house_num, input[name='house']", HOUSE, "house_num")

                page.wait_for_selector("#discon-fact", timeout=20000)

                analysis_script = """
                () => {
                    const updateTimeElem = document.querySelector("#discon-fact > div.discon-fact-info > span.discon-fact-info-text");
                    const updateTime = updateTimeElem ? updateTimeElem.innerText.replace("Дата та час останнього оновлення інформації на графіку:", "").trim() : "---";
                    const row = document.querySelector("#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table > tbody > tr");
                    if (!row) return { update_time: updateTime, schedule: "График не найден" };
                    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
                    let halfStatuses = [];
                    cells.forEach(cell => {
                        let f = cell.classList.contains('cell-scheduled') || cell.classList.contains('cell-first-half');
                        let s = cell.classList.contains('cell-scheduled') || cell.classList.contains('cell-second-half');
                        halfStatuses.push(f ? "🔴" : "🟢"); halfStatuses.push(s ? "🔴" : "🟢");
                    });
                    let intervals = [];
                    let cur = halfStatuses[0]; let start = 0;
                    const fmt = (idx) => {
                        let m = idx * 30;
                        return String(Math.floor(m/60)).padStart(2,'0') + ":" + String(m%60).padStart(2,'0');
                    };
                    for (let i = 1; i <= 48; i++) {
                        if (i === 48 || halfStatuses[i] !== cur) {
                            intervals.push(cur + " <b>" + fmt(start) + " — " + (i === 48 ? "00:00" : fmt(i)) + "</b>");
                            if (i < 48) { cur = halfStatuses[i]; start = i; }
                        }
                    }
                    return { update_time: updateTime, schedule: intervals.join('\\n') };
                }
                """
                # Берем сегодня
                today_data = page.evaluate(analysis_script)

                # Переключаем на завтра
                tomorrow_data = {"update_time": "---", "schedule": "График на завтра еще не опубликован."}
                tomorrow_tab = page.locator("#discon-fact > div.dates > div:nth-child(2)")
                if tomorrow_tab.is_visible():
                    tomorrow_tab.click()
                    tomorrow_data = page.evaluate(analysis_script)

                browser.close()
                return {"today": today_data, "tomorrow": tomorrow_data}
            except Exception as e:
                if 'browser' in locals(): browser.close()
                print(f"Ошибка парсинга: {e}")
                return None

def monitoring_worker(uid, cid):
    """Фоновый мониторинг сразу двух дней"""
    # Первый запуск - запоминаем базу
    res = get_dtek_full_data()
    if res:
        last_known_today[uid] = res['today']['schedule']
        last_known_tomorrow[uid] = res['tomorrow']['schedule']

    while uid in monitoring_users:
        time.sleep(300) 
        if uid not in monitoring_users: break

        res = get_dtek_full_data()
        if not res: continue

        changed = False
        update_msg = "🔔 <b>ВНИМАНИЕ! График изменился:</b>\n\n"

        # Сверяем сегодня
        if res['today']['schedule'] != last_known_today.get(uid):
            last_known_today[uid] = res['today']['schedule']
            update_msg += f"📅 <b>Сегодня:</b>\n{res['today']['schedule']}\n\n"
            changed = True

        # Сверяем завтра
        if res['tomorrow']['schedule'] != last_known_tomorrow.get(uid):
            last_known_tomorrow[uid] = res['tomorrow']['schedule']
            update_msg += f"📅 <b>Завтра:</b>\n{res['tomorrow']['schedule']}\n\n"
            changed = True

        if changed:
            update_msg += f"🕒 <i>Данные с сайта на: {res['today']['update_time']}</i>"
            bot.send_message(cid, update_msg, parse_mode="HTML")

def get_main_markup(uid):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_mon = "Выключить мониторинг ❌" if uid in monitoring_users else "Включить мониторинг 📡"
    markup.add("Сегодня 💡", "Завтра 📅")
    markup.add(btn_mon)
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Бот готов.", reply_markup=get_main_markup(message.from_user.id))

@bot.message_handler(func=lambda m: True)
def handle_all(message):
    uid = message.from_user.id
    cid = message.chat.id
    text = message.text

    if "мониторинг" in text.lower():
        with users_lock:
            if uid in monitoring_users:
                monitoring_users.remove(uid)
                bot.send_message(cid, "📴 Мониторинг выключен.", reply_markup=get_main_markup(uid))
            else:
                monitoring_users.add(uid)
                bot.send_message(cid, "📡 Мониторинг сегодня + завтра включен!", reply_markup=get_main_markup(uid))
                threading.Thread(target=monitoring_worker, args=(uid, cid), daemon=True).start()
        return

    if "Сегодня" in text or "Завтра" in text:
        def task():
            status = bot.send_message(cid, "🔍 Проверяю ДТЭК...")
            res = get_dtek_full_data()
            if res:
                day = "tomorrow" if "Завтра" in text else "today"
                data = res[day]
                resp = f"<b>🕒 Обновлено:</b> {data['update_time']}\n\n<b>📢 График на {text.lower()}:</b>\n\n{data['schedule']}"
                bot.edit_message_text(resp, cid, status.message_id, parse_mode="HTML")
            else:
                bot.edit_message_text("❌ Ошибка связи с сайтом.", cid, status.message_id)
        
        threading.Thread(target=task).start()

if __name__ == "__main__":
    bot.polling(none_stop=True)
