import logging
import sqlite3
import random
import string
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher import Dispatcher
from aiogram.dispatcher.filters import Command
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.storage import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# --- Configuration ---
BOT_TOKEN = "8877608696:AAHKCXkbBytcv-u7qNJvZSlgWO1Z6I8GBUI"
OWNER_ID = 8714064096
ADMIN_IDS = [OWNER_ID]

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Database Setup ---
class Database:
    def __init__(self, db_name="users.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                requests INTEGER DEFAULT 15,
                found_users INTEGER DEFAULT 0,
                promo_used TEXT DEFAULT '',
                registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_bonus_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                requests INTEGER,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS promo_usage (
                user_id INTEGER,
                promo_code TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, promo_code)
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS found_users_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                found_user TEXT,
                length INTEGER,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        self.conn.commit()

    def get_user(self, user_id: int):
        self.cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return self.cursor.fetchone()

    def create_user(self, user_id: int, username: str = "", first_name: str = "", last_name: str = ""):
        try:
            self.cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, requests)
                VALUES (?, ?, ?, ?, 15)
            """, (user_id, username, first_name, last_name))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_user_requests(self, user_id: int, amount: int):
        self.cursor.execute("""
            UPDATE users SET requests = requests + ? WHERE user_id = ?
        """, (amount, user_id))
        self.conn.commit()

    def get_user_requests(self, user_id: int) -> int:
        self.cursor.execute("SELECT requests FROM users WHERE user_id = ?", (user_id,))
        result = self.cursor.fetchone()
        return result[0] if result else 0

    def increment_found_users(self, user_id: int):
        self.cursor.execute("""
            UPDATE users SET found_users = found_users + 1 WHERE user_id = ?
        """, (user_id,))
        self.conn.commit()

    def log_found_user(self, user_id: int, found_user: str, length: int):
        self.cursor.execute("""
            INSERT INTO found_users_log (user_id, found_user, length)
            VALUES (?, ?, ?)
        """, (user_id, found_user, length))
        self.conn.commit()

    def get_user_stats(self, user_id: int):
        user = self.get_user(user_id)
        if user:
            return {
                "user_id": user[0],
                "username": user[1],
                "first_name": user[2],
                "last_name": user[3],
                "requests": user[4],
                "found_users": user[5]
            }
        return None

    def create_promocode(self, code: str, requests: int, max_uses: int, created_by: int):
        try:
            self.cursor.execute("""
                INSERT INTO promocodes (code, requests, max_uses, created_by)
                VALUES (?, ?, ?, ?)
            """, (code, requests, max_uses, created_by))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_promocode(self, code: str):
        self.cursor.execute("SELECT * FROM promocodes WHERE code = ?", (code,))
        return self.cursor.fetchone()

    def use_promocode(self, code: str, user_id: int):
        promo = self.get_promocode(code)
        if not promo:
            return False, "Промокод не найден!"
        
        self.cursor.execute("SELECT * FROM promo_usage WHERE user_id = ? AND promo_code = ?", (user_id, code))
        if self.cursor.fetchone():
            return False, "Вы уже использовали этот промокод!"
        
        if promo[3] != -1 and promo[4] >= promo[3]:
            return False, "Промокод больше не действителен!"
        
        self.cursor.execute("""
            UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?
        """, (code,))
        self.cursor.execute("""
            INSERT INTO promo_usage (user_id, promo_code) VALUES (?, ?)
        """, (user_id, code))
        self.update_user_requests(user_id, promo[2])
        self.conn.commit()
        return True, f"Промокод активирован! Вы получили {promo[2]} запросов!"

    def get_all_users(self):
        self.cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in self.cursor.fetchall()]

    def get_daily_requests_bonus(self, user_id: int) -> int:
        self.cursor.execute("""
            SELECT last_bonus_date FROM users WHERE user_id = ?
        """, (user_id,))
        result = self.cursor.fetchone()
        if not result:
            return 0
        
        last_bonus = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        
        if (now - last_bonus).total_seconds() >= 86400:
            self.update_user_requests(user_id, 2)
            self.cursor.execute("""
                UPDATE users SET last_bonus_date = CURRENT_TIMESTAMP WHERE user_id = ?
            """, (user_id,))
            self.conn.commit()
            return 2
        return 0

db = Database()

# --- States ---
class PromoStates(StatesGroup):
    waiting_for_promo = State()

# --- Bot & Dispatcher ---
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# --- Helper Functions ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def generate_random_username(length: int) -> str:
    letters = string.ascii_lowercase
    return ''.join(random.choice(letters) for _ in range(length))

def get_main_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search"),
        InlineKeyboardButton(text="📋 Задание", callback_data="tasks"),
        InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        InlineKeyboardButton(text="🛒 Магазин", callback_data="shop"),
        InlineKeyboardButton(text="🎟️ Промокод", callback_data="promo"),
        InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals"),
    ]
    keyboard.add(*buttons)
    
    if is_admin(OWNER_ID):
        keyboard.add(InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin_panel"))
    
    return keyboard

def get_length_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(text="5 букв", callback_data="length_5"),
        InlineKeyboardButton(text="6 букв", callback_data="length_6"),
        InlineKeyboardButton(text="7 букв", callback_data="length_7"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
    ]
    keyboard.add(*buttons)
    return keyboard

def get_shop_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    shop_items = [
        ("10 запросов = 5 ⭐", "shop_10"),
        ("15 запросов = 10 ⭐", "shop_15"),
        ("20 запросов = 15 ⭐", "shop_20"),
        ("50 запросов = 40 ⭐", "shop_50"),
        ("100 запросов = 90 ⭐", "shop_100"),
        ("250 запросов = 200 ⭐", "shop_250"),
        ("500 запросов = 449 ⭐", "shop_500"),
        ("750 запросов = 699 ⭐", "shop_750"),
        ("899 запросов = 849 ⭐", "shop_899"),
        ("1000 запросов = 888 ⭐", "shop_1000"),
        ("🔙 Назад", "back_to_main")
    ]
    for text, callback in shop_items:
        keyboard.add(InlineKeyboardButton(text=text, callback_data=callback))
    return keyboard

def get_admin_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    buttons = [
        InlineKeyboardButton(text="➕ Добавить запросы", callback_data="admin_give"),
        InlineKeyboardButton(text="➖ Удалить запросы", callback_data="admin_delete"),
        InlineKeyboardButton(text="🎟️ Create Promo", callback_data="admin_create_promo"),
    ]
    
    if is_admin(OWNER_ID):
        buttons.append(InlineKeyboardButton(text="➕ Hack", callback_data="admin_hack_plus"))
        buttons.append(InlineKeyboardButton(text="➖ Hack", callback_data="admin_hack_minus"))
    
    buttons.append(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    for btn in buttons:
        keyboard.add(btn)
    return keyboard

# --- Handlers ---
@dp.message_handler(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    
    if not db.get_user(user_id):
        db.create_user(user_id, username, first_name, last_name)
    
    bonus = db.get_daily_requests_bonus(user_id)
    bonus_text = f"\n\n🎁 Вы получили +{bonus} запросов за сегодня!" if bonus > 0 else ""
    
    welcome_text = (
        f"👋 Добро пожаловать в @Searchusegen_bot!\n"
        f"🔍 Здесь вы можете находить свободные юзернеймы.\n"
        f"📊 У вас {db.get_user_requests(user_id)} запросов.{bonus_text}\n\n"
        f"Выберите действие:"
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.callback_query_handler(lambda c: c.data == "back_to_main")
async def back_to_main(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    bonus = db.get_daily_requests_bonus(user_id)
    bonus_text = f"\n\n🎁 Вы получили +{bonus} запросов за сегодня!" if bonus > 0 else ""
    
    text = (
        f"👋 Главное меню\n"
        f"📊 У вас {db.get_user_requests(user_id)} запросов.{bonus_text}\n\n"
        f"Выберите действие:"
    )
    await bot.edit_message_text(text, callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_main_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "search")
async def cmd_search(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    requests_left = db.get_user_requests(user_id)
    
    if requests_left <= 0:
        await callback_query.answer("❌ У вас нет запросов! Дождитесь следующего дня.", show_alert=True)
        return
    
    await bot.edit_message_text(
        "🔍 Выберите длину юзернейма:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_length_keyboard()
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("length_"))
async def search_by_length(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    length = int(callback_query.data.split("_")[1])
    
    if db.get_user_requests(user_id) <= 0:
        await callback_query.answer("❌ У вас нет запросов!", show_alert=True)
        return
    
    username = generate_random_username(length)
    db.update_user_requests(user_id, -1)
    db.increment_found_users(user_id)
    db.log_found_user(user_id, username, length)
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton(text="🔍 Еще поиск", callback_data="search"),
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")
    )
    
    await bot.edit_message_text(
        f"✅ Найден юзернейм:\n\n@{username}\n\n"
        f"📊 Осталось запросов: {db.get_user_requests(user_id)}",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "profile")
async def cmd_profile(callback_query: types.CallbackQuery):
    stats = db.get_user_stats(callback_query.from_user.id)
    if not stats:
        await callback_query.answer("❌ Ошибка!", show_alert=True)
        return
    
    profile_text = (
        f"👤 Профиль\n\n"
        f"1️⃣ Имя: {stats['first_name']} {stats['last_name'] or ''}\n"
        f"2️⃣ ID: {stats['user_id']}\n"
        f"3️⃣ Найдено юзов: {stats['found_users']}\n"
        f"4️⃣ Запросов: {stats['requests']}"
    )
    
    await bot.edit_message_text(profile_text, callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_main_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "shop")
async def cmd_shop(callback_query: types.CallbackQuery):
    shop_text = (
        "🛒 Магазин запросов\n\n"
        "Для покупки пишите @Haremilove\n"
        "Одним сообщением, спам = ЧС!\n\n"
        "💰 Цены:\n"
        "1. 10 запросов = 5 ⭐\n"
        "2. 15 запросов = 10 ⭐\n"
        "3. 20 запросов = 15 ⭐\n"
        "4. 50 запросов = 40 ⭐\n"
        "5. 100 запросов = 90 ⭐\n"
        "6. 250 запросов = 200 ⭐\n"
        "7. 500 запросов = 449 ⭐\n"
        "8. 750 запросов = 699 ⭐\n"
        "9. 899 запросов = 849 ⭐\n"
        "10. 1000 запросов = 888 ⭐"
    )
    
    await bot.edit_message_text(shop_text, callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_shop_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "promo")
async def cmd_promo(callback_query: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    
    await bot.edit_message_text(
        "🎟️ Введите промокод:",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )
    await PromoStates.waiting_for_promo.set()
    await callback_query.answer()

@dp.message_handler(state=PromoStates.waiting_for_promo)
async def process_promo(message: types.Message, state: FSMContext):
    success, result = db.use_promocode(message.text.strip(), message.from_user.id)
    
    await message.answer(
        f"{'✅' if success else '❌'} {result}\n"
        f"📊 У вас {db.get_user_requests(message.from_user.id)} запросов.",
        reply_markup=get_main_keyboard()
    )
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "support")
async def cmd_support(callback_query: types.CallbackQuery):
    await bot.edit_message_text(
        "🆘 Поддержка\n\nПо всем вопросам: @Haremilove",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=get_main_keyboard()
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "tasks")
async def cmd_tasks(callback_query: types.CallbackQuery):
    await bot.edit_message_text("📋 В разработке...", callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_main_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "referrals")
async def cmd_referrals(callback_query: types.CallbackQuery):
    await bot.edit_message_text("👥 В разработке...", callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_main_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_panel")
async def cmd_admin_panel(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await bot.edit_message_text("⚙️ Admin Panel", callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_admin_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_give")
async def admin_give(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    
    await bot.edit_message_text(
        "➕ Добавить запросы\n\n/give @username количество",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.message_handler(Command("give"))
async def cmd_give(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен!")
        return
    
    args = message.text.split()
    if len(args) != 3:
        await message.answer("❌ Использование: /give @username количество")
        return
    
    username = args[1].replace("@", "")
    try:
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Количество должно быть числом!")
        return
    
    db.cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
    result = db.cursor.fetchone()
    
    if not result:
        await message.answer(f"❌ Пользователь @{username} не найден!")
        return
    
    target_id = result[0]
    db.update_user_requests(target_id, amount)
    await message.answer(f"✅ Выдано {amount} запросов @{username}")
    
    try:
        await bot.send_message(
            target_id,
            f"🎁 @Haremilove подарил вам {amount} запросов!"
        )
    except:
        pass

@dp.callback_query_handler(lambda c: c.data == "admin_delete")
async def admin_delete(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    
    await bot.edit_message_text(
        "➖ Удалить запросы\n\n/delete @username количество",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.message_handler(Command("delete"))
async def cmd_delete(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен!")
        return
    
    args = message.text.split()
    if len(args) != 3:
        await message.answer("❌ Использование: /delete @username количество")
        return
    
    username = args[1].replace("@", "")
    try:
        amount = int(args[2])
    except ValueError:
        await message.answer("❌ Количество должно быть числом!")
        return
    
    db.cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
    result = db.cursor.fetchone()
    
    if not result:
        await message.answer(f"❌ Пользователь @{username} не найден!")
        return
    
    target_id = result[0]
    current = db.get_user_requests(target_id)
    new_amount = max(0, current - amount)
    db.update_user_requests(target_id, -(current - new_amount))
    await message.answer(f"✅ Удалено {amount} запросов у @{username}")

@dp.callback_query_handler(lambda c: c.data == "admin_create_promo")
async def admin_create_promo(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    
    await bot.edit_message_text(
        "🎟️ Создание промокода\n\n/createpromo название кол-во [использований]",
        callback_query.message.chat.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.message_handler(Command("createpromo"))
async def cmd_create_promo(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен!")
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Использование: /createpromo название кол-во [использований]")
        return
    
    code = args[1]
    try:
        requests = int(args[2])
    except ValueError:
        await message.answer("❌ Количество запросов должно быть числом!")
        return
    
    max_uses = -1
    if len(args) >= 4:
        try:
            max_uses = int(args[3])
        except ValueError:
            await message.answer("❌ Количество использований должно быть числом!")
            return
    
    if db.create_promocode(code, requests, max_uses, message.from_user.id):
        await message.answer(f"✅ Промокод {code} создан!")
        
        users = db.get_all_users()
        for user_id in users:
            try:
                await bot.send_message(
                    user_id,
                    f"🎟️ НОВЫЙ ПРОМОКОД 🎟️\n\n"
                    f"🎟️ {code}\n"
                    f"🎁 +{requests} запросов\n"
                    f"⚡ Всего: {'∞' if max_uses == -1 else max_uses}\n"
                    f"📌 Каждый день — новый промокод!"
                )
                await asyncio.sleep(0.05)
            except:
                pass
    else:
        await message.answer("❌ Промокод уже существует!")

@dp.callback_query_handler(lambda c: c.data == "admin_hack_plus")
async def admin_hack_plus(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != OWNER_ID:
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    for user_id in db.get_all_users():
        db.update_user_requests(user_id, 1000000000)
    
    await bot.edit_message_text("✅ +1.000.000.000 всем!", callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_admin_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_hack_minus")
async def admin_hack_minus(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != OWNER_ID:
        await callback_query.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    for user_id in db.get_all_users():
        db.update_user_requests(user_id, -1000000000)
    
    await bot.edit_message_text("✅ -1.000.000.000 всем!", callback_query.message.chat.id, callback_query.message.message_id, reply_markup=get_admin_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("shop_"))
async def shop_purchase(callback_query: types.CallbackQuery):
    await callback_query.answer("📩 Пишите @Haremilove", show_alert=True)

# --- Startup ---
async def init_promo():
    if not db.get_promocode("Test"):
        db.create_promocode("Test", 1000000000000000000000, 1, OWNER_ID)
        logger.info("✅ Промокод Test создан")

async def on_startup(dp):
    await init_promo()
    logger.info("✅ Бот запущен!")

# --- Main ---
if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
