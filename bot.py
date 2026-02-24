import time

import telebot

import threading

import re

from playwright.sync_api import sync_playwright



# --- КОНФИГУРАЦИЯ ---

TOKEN = "8702758834:AAHbQNtVyNl85z2xtPiuHlAbUfPSBqtCshA"

bot = telebot.TeleBot(TOKEN)



# Глобальные объекты управления

active_users = set()       # Кто сейчас в процессе ручного запроса

monitoring_users = set()   # У кого включен авто-мониторинг

last_known_data = {}       # Последние данные для сравнения (user_id: text)

last_request_time = {}     # Время последнего клика для КД

users_lock = threading.Lock()

browser_lock = threading.Lock() # Строго по очереди для стабильности



def get_dtek_analysis(day_type="today"):

    """Запуск браузера и парсинг данных с маскировкой под человека"""

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




                # Заполнение адреса

                safe_fill(page, "input[name='city']", "с. Мала Михайлівка", "city")

                safe_fill(page, "input[name='street']", "вул. Бесарабська", "street")

                safe_fill(page, "input#house_num, input[name='house']", "32/", "house_num")



                # Ждем таблицу

                page.wait_for_selector("#discon-fact", timeout=20000)

                

                if day_type == "tomorrow":

                    tab = page.locator("#discon-fact > div.dates > div:nth-child(2)")

                    if not tab.is_visible(): return {"update_time": "Неизвестно", "schedule": "График на завтра еще не опубликован."}

                    tab.click(force=True)

                    time.sleep(2)



                # JS АНАЛИЗАТОР (Дата обновления + График)

# JS АНАЛИЗАТОР (Полный график с 🟢 и 🔴)
                analysis_script = """
                () => {
                    const updateTimeElem = document.querySelector("#discon-fact > div.discon-fact-info > span.discon-fact-info-text");
                    const updateTime = updateTimeElem ? updateTimeElem.innerText.replace("Дата та час останнього оновлення інформації на графіку:", "").trim() : "Неизвестно";

                    const row = document.querySelector("#discon-fact > div.discon-fact-tables > div.discon-fact-table.active > table > tbody > tr");
                    if (!row) return { update_time: updateTime, schedule: "График не найден." };
                    
                    const cells = Array.from(row.querySelectorAll("td")).slice(1, 25);
                    let scheduleText = "";

                    cells.forEach((cell, hour) => {
                        let statusEmoji = "🟢"; // По умолчанию свет есть
                        
                        // Проверяем наличие классов отключений
                        if (cell.classList.contains('cell-scheduled') || 
                            cell.classList.contains('cell-first-half') || 
                            cell.classList.contains('cell-second-half')) {
                            statusEmoji = "🔴"; // Света нет
                        }

                        // Форматируем строку: "00:00 🟢"
                        let hourStr = hour.toString().padStart(2, '0') + ":00";
                        scheduleText += hourStr + " " + statusEmoji + "\\n";
                    });

                    return { update_time: updateTime, schedule: scheduleText };
                }
                """
                result = page.evaluate(analysis_script)

                browser.close()

                return result

            except Exception as e:

                browser.close()

                return {"update_time": "Ошибка", "schedule": f"Ошибка: {str(e)}"}



def monitoring_worker(uid, cid):

    """Фоновая задача проверки обновлений раз в 5 минут"""

    while uid in monitoring_users:

        try:

            data = get_dtek_analysis("today")

            full_text = f"🕒 <b>Обновлено на сайте:</b> {data['update_time']}\n\n{data['schedule']}"

            

            # Если данные изменились - уведомляем

            if uid not in last_known_data or last_known_data[uid] != full_text:

                last_known_data[uid] = full_text

                bot.send_message(cid, f"🔔 <b>ВНИМАНИЕ! График изменился:</b>\n\n{full_text}", parse_mode="HTML")

        except: pass

        time.sleep(300) # 5 минут



@bot.message_handler(commands=['start'])

def start(message):

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add("Сегодня 💡", "Завтра 📅", "Мониторинг 📡")

    bot.send_message(message.chat.id, "Бот готов. Выберите действие:", reply_markup=markup)



@bot.message_handler(func=lambda m: True)

def handle_all(message):

    uid = message.from_user.id

    cid = message.chat.id

    text = message.text



    # 1. Логика кнопки Мониторинг

    if text == "Мониторинг 📡":

        with users_lock:

            if uid in monitoring_users:

                monitoring_users.remove(uid)

                bot.reply_to(message, "📴 Мониторинг выключен.")

            else:

                monitoring_users.add(uid)

                bot.reply_to(message, "📡 Мониторинг включен! Проверяю каждые 5 минут. Пришлю сообщение, если график изменится.")

                threading.Thread(target=monitoring_worker, args=(uid, cid), daemon=True).start()

        return



    # 2. Проверка на мусорные сообщения

    if not any(x in text for x in ["Сегодня", "Завтра", "💡", "📅"]):

        bot.reply_to(message, "🤖 Нажми на кнопки Сегодня/Завтра или Мониторинг.")

        return



    # 3. Кулдаун 10 секунд

    now = time.time()

    if uid in last_request_time and now - last_request_time[uid] < 10:

        bot.reply_to(message, f"⚠️ Подожди {int(10 - (now - last_request_time[uid]))} сек.")

        return



    # 4. Запуск парсинга в отдельном потоке

    with users_lock:

        if uid in active_users:

            bot.reply_to(message, "⏳ Твой запрос уже в очереди!")

            return

        active_users.add(uid)



    last_request_time[uid] = now



    def task():

        try:

            day = "tomorrow" if "Завтра" in text else "today"

            status = bot.send_message(cid, f"🔍 Считываю таблицу (в очереди)...")

            

            data = get_dtek_analysis(day)

            response = f"<b>🕒 Обновлено:</b> {data['update_time']}\n\n<b>📢 График на {text.lower()}:</b>\n\n{data['schedule']}"

            

            bot.edit_message_text(response, cid, status.message_id, parse_mode="HTML")

            last_known_data[uid] = response # Запоминаем для мониторинга

        except Exception as e:

            bot.send_message(cid, f"❌ Ошибка: {e}")

        finally:

            with users_lock:

                if uid in active_users: active_users.remove(uid)



    threading.Thread(target=task).start()



if __name__ == "__main__":
    bot.polling(none_stop=True)
