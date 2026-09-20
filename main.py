import sqlite3
import time
import threading
import urllib.parse
from bs4 import BeautifulSoup
from curl_cffi import requests
import telebot

# ==========================================
# НАСТРОЙКИ: ТВОЙ ТОКЕН УЖЕ ВПИСАН В КОД!
# ==========================================
BOT_TOKEN = "8937120751:AAHopHE6rmL0pjva4iSkmktJF4e_hTAzDTE"

# Подключаем бота
bot = telebot.TeleBot(BOT_TOKEN)

# Слова, которые бот ищет в названии. Если они есть — объявление пропускается!
BAD_WORDS = ["запчасти", "дефект", "битый", "полоса", "не рабочий", "ремонт", "трещина", "разбит"]


# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (Где бот всё хранит)
# ==========================================
def init_db():
    """Создает таблицы в базе данных, если их еще нет."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    # Таблица для поисков
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            query TEXT,
            city TEXT,
            max_price INTEGER
        )
    """)
    # Таблица для уже отправленных объявлений (чтобы не присылать дважды)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_ads (
            task_id INTEGER,
            ad_id TEXT,
            PRIMARY KEY (task_id, ad_id)
        )
    """)
    conn.commit()
    conn.close()

def add_task(user_id, query, city, max_price):
    """Добавляет новый поиск в базу."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (user_id, query, city, max_price) VALUES (?, ?, ?, ?)",
        (user_id, query.lower(), city.lower(), max_price)
    )
    conn.commit()
    conn.close()

def get_all_tasks():
    """Получает список всех поисков всех пользователей."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, query, city, max_price FROM tasks")
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def delete_user_tasks(user_id):
    """Удаляет все поиски пользователя."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_ad_sent(task_id, ad_id):
    """Проверяет, отправляли ли мы уже это объявление."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_ads WHERE task_id = ? AND ad_id = ?", (task_id, ad_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_ad_as_sent(task_id, ad_id):
    """Записывает объявление как отправленное."""
    conn = sqlite3.connect("avito_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO sent_ads (task_id, ad_id) VALUES (?, ?)", (task_id, ad_id))
    conn.commit()
    conn.close()


# ==========================================
# ПАРСЕР АВИТО (Запросы на сайт и анализ цен)
# ==========================================
def parse_avito(query, city):
    """Ищет объявления на Авито по запросу."""
    encoded_query = urllib.parse.quote(query)
    # s=104 означает сортировку по дате (сначала новые)
    url = f"https://www.avito.ru/{city}?q={encoded_query}&s=104"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }

    try:
        # curl_cffi маскируется под реальный браузер Chrome, чтобы Авито нас не забанил
        response = requests.get(url, headers=headers, impersonate="chrome110", timeout=10)
        if response.status_code != 200:
            print(f"[-] Авито вернул ошибку {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        ads = []
        
        # Находим все блоки объявлений на странице
        blocks = soup.select('[data-marker="item"]')
        for block in blocks:
            try:
                ad_id = block.get("data-item-id")
                
                title_elem = block.select_one('[data-marker="item-title"]')
                title = title_elem.text.strip()
                link = "https://www.avito.ru" + title_elem.get("href")

                price_elem = block.select_one('[data-marker="item-price"]')
                # Очищаем цену от пробелов и знака рубля
                price_text = price_elem.text.replace("\xa0", "").replace(" ", "").replace("₽", "").strip()
                price = int(price_text) if price_text.isdigit() else 0

                # Фильтруем плохие слова
                title_lower = title.lower()
                if any(bad in title_lower for bad in BAD_WORDS):
                    continue

                if price > 0:
                    ads.append({
                        "id": ad_id,
                        "title": title,
                        "price": price,
                        "url": link
                    })
            except Exception:
                continue
                
        return ads
    except Exception as e:
        print(f"[-] Ошибка сети: {e}")
        return []


def check_is_cheap(ads, current_ad, max_price):
    """Решает, выгодно ли это объявление."""
    price = current_ad["price"]
    
    # Если мы сами указали максимальную цену (например, 18000), и товар дешевле — берем!
    if max_price > 0:
        if price <= max_price:
            return True
        return False # Если дороже лимита, то не берем
        
    # Если лимит цены не указан (ввели 0), бот ищет скидку 20% от средней цены в списке
    prices = [ad["price"] for ad in ads]
    if len(prices) >= 3:
        avg_price = sum(prices) / len(prices)
        if price <= avg_price * 0.80: # Дешевле средней цены на 20%
            return True
            
    return False


# ==========================================
# ФОНОВЫЙ ПОИСК (Работает 24/7)
# ==========================================
def monitoring_thread():
    """Этот цикл крутится постоянно и раз в пару минут проверяет Авито."""
    print("[+] Фоновый мониторинг запущен!")
    while True:
        try:
            tasks = get_all_tasks()
            for task in tasks:
                task_id, user_id, query, city, max_price = task
                print(f"[~] Проверяем: {query} в {city}...")
                
                ads = parse_avito(query, city)
                
                for ad in ads:
                    # Если объявление уже отправляли — пропускаем
                    if is_ad_sent(task_id, ad["id"]):
                        continue
                        
                    # Проверяем выгоду
                    if check_is_cheap(ads, ad, max_price):
                        text = (
                            f"🔔 **НАЙДЕНО ВЫГОДНОЕ ОБЪЯВЛЕНИЕ!**\n\n"
                            f"📦 **Товар:** {ad['title']}\n"
                            f"💰 **Цена:** {ad['price']:,} руб.\n"
                            f"📍 **Поиск:** {query} ({city})\n\n"
                            f"🔗 {ad['url']}"
                        )
                        try:
                            bot.send_message(user_id, text, parse_mode="Markdown")
                        except Exception as e:
                            print(f"[-] Ошибка отправки сообщения в ТГ: {e}")
                            
                    # Запоминаем, что мы его обработали (чтобы не спамить)
                    mark_ad_as_sent(task_id, ad["id"])
                    
                # Спим 10 секунд между разными запросами, чтобы Авито не ругался
                time.sleep(10)
                
        except Exception as e:
            print(f"[-] Ошибка в фоновом режиме: {e}")
            
        # Как часто бот заходит на Авито (сейчас раз в 3 минуты = 180 секунд)
        time.sleep(180)


# ==========================================
# КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЯ В TELEGRAM
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    help_text = (
        "👋 Привет! Я твой личный охотник на Авито!\n\n"
        "**Как добавить новый поиск?**\n"
        "Отправь мне сообщение такого вида:\n"
        "`Ищи: Монитор 27 144гц | moskva | 18000`\n\n"
        "*(Где 'moskva' — город на латинице из адресной строки Авито, "
        "а '18000' — твоя максимальная цена. Если поставить '0', бот сам найдет товары со скидкой 20%+ от средней цены рынка)*\n\n"
        "❌ Чтобы удалить все твои поиски, напиши: `/stop`"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['stop'])
def stop_monitoring(message):
    delete_user_tasks(message.from_user.id)
    bot.reply_to(message, "🗑 Все твои поиски удалены! Бот больше ничего не ищет.")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    text = message.text
    if "ищи:" in text.lower():
        try:
            # Парсим сообщение пользователя по разделителю "|"
            parts = text.split("|")
            query = parts[0].replace("Ищи:", "").replace("ищи:", "").strip()
            city = parts[1].strip()
            max_price = int(parts[2].strip())
            
            # Добавляем в базу данных
            add_task(message.from_user.id, query, city, max_price)
            
            price_text = f"до {max_price} руб." if max_price > 0 else "скидки от 20% от рынка"
            bot.reply_to(
                message, 
                f"✅ **Поиск запущен!**\n\n"
                f"🔎 Ищем: `{query}`\n"
                f"🏙 Город: `{city}`\n"
                f"💵 Фильтр: {price_text}\n\n"
                f"Я буду проверять Авито каждые 3 минуты и присылать ссылки сюда!",
                parse_mode="Markdown"
            )
        except Exception:
            bot.reply_to(
                message, 
                "❌ Неверный формат! Напиши строго как в примере:\n"
                "`Ищи: Монитор 27 144гц | moskva | 18000`", 
                parse_mode="Markdown"
            )
    else:
        bot.reply_to(message, "Я не понял. Чтобы запустить поиск, напиши, например:\n`Ищи: Монитор 27 144гц | moskva | 18000`")


# ==========================================
# ЗАПУСК ВСЕЙ СИСТЕМЫ
# ==========================================
if __name__ == "__main__":
    init_db()
    # Запускаем фоновый поток для парсинга Авито
    t = threading.Thread(target=monitoring_thread)
    t.daemon = True
    t.start()
    
    # Запускаем самого бота Telegram
    print("[+] Телеграм бот успешно запущен и ждет команд!")
    bot.infinity_polling()
