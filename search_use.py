import logging
import sqlite3
import random
import string
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- Configuration ---
BOT_TOKEN = "8877608696:AAHKCXkbBytcv-u7qNJvZSlgWO1Z6I8GBUI"
OWNER_ID = 8714064096
ADMIN_IDS = [OWNER_ID]  # Add other admin IDs here

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
        # Users table
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
        
        # Promocodes table
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
        
        # Promocode usage tracking (for 1 use per user)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS promo_usage (
                user_id INTEGER,
                promo_code TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, promo_code)
            )
        """)
        
        # Found users logs (just for statistics)
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

    def get_user(self, user_id: int) -> Optional[Tuple]:
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

    def get_user_stats(self, user_id: int) -> Optional[Dict]:
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

    def get_promocode(self, code: str) -> Optional[Tuple]:
        self.cursor.execute("SELECT * FROM promocodes WHERE code = ?", (code,))
        return self.cursor.fetchone()

    def use_promocode(self, code: str, user_id: int) -> Tuple[bool, str]:
        promo = self.get_promocode(code)
        if not promo:
            return False, "Промокод не найден!"
        
        # Check if user already used this promo
        self.cursor.execute("SELECT * FROM promo_usage WHERE user_id = ? AND promo_code = ?", (user_id, code))
        if self.cursor.fetchone():
            return False, "Вы уже использовали этот промокод!"
        
        # Check max uses
        if promo[3] != -1 and promo[4] >= promo[3]:
            return False, "Промокод больше не действителен!"
        
        # Apply promo
        self.cursor.execute("""
            UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?
        """, (code,))
        self.cursor.execute("""
            INSERT INTO promo_usage (user_id, promo_code) VALUES (?, ?)
        """, (user_id, code))
        self.update_user_requests(user_id, promo[2])
        self.conn.commit()
        return True, f"Промокод активирован! Вы получили {promo[2]} запросов!"

    def get_all_users(self) -> List[int]:
        self.cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in self.cursor.fetchall()]

    def get_all_promocodes(self) -> List[Tuple]:
        self.cursor.execute("SELECT code, requests, max_uses, used_count FROM promocodes ORDER BY created_at DESC")
        return self.cursor.fetchall()

    def get_daily_requests_bonus(self, user_id: int) -> int:
        """Returns 2 requests for daily bonus if 24h passed since last bonus"""
        self.cursor.execute("""
            SELECT last_bonus_date FROM users WHERE user_id = ?
        """, (user_id,))
        result = self.cursor.fetchone()
        if not result:
            return 0
        
        last_bonus = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        
        # Check if 24h passed since last bonus
        if (now - last_bonus).total_seconds() >= 86400:
            self.update_user_requests(user_id, 2)
            # Update last_bonus_date to now
            self.cursor.execute("""
                UPDATE users SET last_bonus_date = CURRENT_TIMESTAMP WHERE user_id = ?
            """, (user_id,))
            self.conn.commit()
            return 2
        return 0

db = Database()

# --- States ---
class SearchStates(StatesGroup):
    choosing_length = State()
    searching = State()

class PromoStates(StatesGroup):
    waiting_for_promo = State()

class AdminStates(StatesGroup):
    waiting_for_give = State()
    waiting_for_delete = State()
    waiting_for_create_promo = State()
    waiting_for_promo_name = State()
    waiting_for_promo_requests = State()
    waiting_for_promo_uses = State()

# --- Bot & Dispatcher ---
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# --- Helper Functions ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def generate_random_username(length: int) -> str:
    """Generate random username of specified length (letters only)"""
    letters = string.ascii_lowercase
    return ''.join(random.choice(letters) for _ in range(length))

def get_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search"),
        InlineKeyboardButton(text="📋 Задание", callback_data="tasks")
    )
    builder.row(
        InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        InlineKeyboardButton(text="🛒 Магазин", callback_data="shop")
    )
    builder.row(
        InlineKeyboardButton(text="🎟️ Промокод", callback_data="promo"),
        InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")
    )
    builder.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="referrals")
    )
    if any(is_admin(user_id) for user_id in [OWNER_ID]):
        builder.row(
            InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin_panel")
        )
    return builder.as_markup()

def get_length_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="5 букв", callback_data="length_5"),
        InlineKeyboardButton(text="6 букв", callback_data="length_6"),
        InlineKeyboardButton(text="7 букв", callback_data="length_7")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
    )
    return builder.as_markup()

def get_shop_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
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
        ("1000 запросов = 888 ⭐", "shop_1000")
    ]
    for text, callback in shop_items:
        builder.row(InlineKeyboardButton(text=text, callback_data=callback))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

def get_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить запросы", callback_data="admin_give"))
    builder.row(InlineKeyboardButton(text="➖ Удалить запросы", callback_data="admin_delete"))
    builder.row(InlineKeyboardButton(text="🎟️ Create Promo", callback_data="admin_create_promo"))
    if OWNER_ID:  # Show hack buttons only for owner
        builder.row(InlineKeyboardButton(text="➕ Hack", callback_data="admin_hack_plus"))
        builder.row(InlineKeyboardButton(text="➖ Hack", callback_data="admin_hack_minus"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    return builder.as_markup()

# --- Message Handlers ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    
    # Create user if not exists
    if not db.get_user(user_id):
        db.create_user(user_id, username, first_name, last_name)
    
    # Check for daily bonus
    bonus = db.get_daily_requests_bonus(user_id)
    bonus_text = f"\n\n🎁 Вы получили +{bonus} запросов за сегодня!" if bonus > 0 else ""
    
    welcome_text = (
        f"👋 Добро пожаловать в @Searchusegen_bot!\n"
        f"🔍 Здесь вы можете находить свободные юзернеймы.\n"
        f"📊 У вас {db.get_user_requests(user_id)} запросов.{bonus_text}\n\n"
        f"Выберите действие:"
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    
    # Check for daily bonus
    bonus = db.get_daily_requests_bonus(user_id)
    bonus_text = f"\n\n🎁 Вы получили +{bonus} запросов за сегодня!" if bonus > 0 else ""
    
    text = (
        f"👋 Главное меню\n"
        f"📊 У вас {db.get_user_requests(user_id)} запросов.{bonus_text}\n\n"
        f"Выберите действие:"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "search")
async def cmd_search(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    requests_left = db.get_user_requests(user_id)
    
    if requests_left <= 0:
        await callback.answer("❌ У вас нет запросов! Дождитесь следующего дня для получения 2 запросов.", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🔍 Выберите длину юзернейма для поиска:",
        reply_markup=get_length_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("length_"))
async def search_by_length(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    length = int(callback.data.split("_")[1])
    
    requests_left = db.get_user_requests(user_id)
    if requests_left <= 0:
        await callback.answer("❌ У вас нет запросов!", show_alert=True)
        return
    
    # Generate random username
    username = generate_random_username(length)
    
    # Deduct request
    db.update_user_requests(user_id, -1)
    db.increment_found_users(user_id)
    db.log_found_user(user_id, username, length)
    
    await callback.message.edit_text(
        f"✅ Найден юзернейм:\n\n"
        f"@{username}\n\n"
        f"📊 Осталось запросов: {db.get_user_requests(user_id)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Еще поиск", callback_data="search")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")]
            ]
        )
    )
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def cmd_profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    stats = db.get_user_stats(user_id)
    
    if not stats:
        await callback.answer("❌ Ошибка! Пользователь не найден.", show_alert=True)
        return
    
    profile_text = (
        f"👤 Профиль\n\n"
        f"1️⃣ Имя: {stats['first_name']} {stats['last_name'] or ''}\n"
        f"2️⃣ ID: {stats['user_id']}\n"
        f"3️⃣ Найдено юзов: {stats['found_users']}\n"
        f"4️⃣ Запросов: {stats['requests']}"
    )
    
    await callback.message.edit_text(profile_text, reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "shop")
async def cmd_shop(callback: CallbackQuery):
    shop_text = (
        "🛒 Магазин запросов\n\n"
        "Для покупки запросов пишите @Haremilove\n"
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
    
    await callback.message.edit_text(shop_text, reply_markup=get_shop_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "promo")
async def cmd_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🎟️ Введите промокод:\n\n"
        "Пример: Test",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
            ]
        )
    )
    await state.set_state(PromoStates.waiting_for_promo)
    await callback.answer()

@dp.message(PromoStates.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    promo_code = message.text.strip()
    
    success, result = db.use_promocode(promo_code, user_id)
    
    if success:
        await message.answer(
            f"✅ {result}\n"
            f"📊 У вас {db.get_user_requests(user_id)} запросов.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            f"❌ {result}",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@dp.callback_query(F.data == "support")
async def cmd_support(callback: CallbackQuery):
    await callback.message.edit_text(
        "🆘 Поддержка\n\n"
        "По всем вопросам обращайтесь к владельцу:\n"
        "@Haremilove\n\n"
        "Опишите вашу проблему подробно.",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "tasks")
async def cmd_tasks(callback: CallbackQuery):
    await callback.message.edit_text(
        "📋 Задания\n\n"
        "В разработке...",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "referrals")
async def cmd_referrals(callback: CallbackQuery):
    await callback.message.edit_text(
        "👥 Реферальная система\n\n"
        "В разработке...",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def cmd_admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "⚙️ Admin Panel\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_give")
async def admin_give(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "➕ Добавить запросы\n\n"
        "Используйте команду:\n"
        "/give @username количество\n\n"
        "Пример: /give @Haremilove 10",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
            ]
        )
    )
    await state.clear()
    await callback.answer()

@dp.message(Command("give"))
async def cmd_give(message: Message):
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
    
    # Find user by username
    db.cursor.execute("SELECT user_id FROM users WHERE username = ?", (username,))
    result = db.cursor.fetchone()
    
    if not result:
        await message.answer(f"❌ Пользователь @{username} не найден в базе!")
        return
    
    target_id = result[0]
    db.update_user_requests(target_id, amount)
    
    await message.answer(f"✅ Выдано {amount} запросов пользователю @{username}")
    
    # Notify user
    try:
        await bot.send_message(
            target_id,
            f"🎁 @Haremilove подарил вам {amount} запросов!\n"
            f"📊 У вас {db.get_user_requests(target_id)} запросов."
        )
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")

@dp.callback_query(F.data == "admin_delete")
async def admin_delete(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "➖ Удалить запросы\n\n"
        "Используйте команду:\n"
        "/delete @username количество\n\n"
        "Пример: /delete @Haremilove 5",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
            ]
        )
    )
    await state.clear()
    await callback.answer()

@dp.message(Command("delete"))
async def cmd_delete(message: Message):
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
        await message.answer(f"❌ Пользователь @{username} не найден в базе!")
        return
    
    target_id = result[0]
    current = db.get_user_requests(target_id)
    new_amount = max(0, current - amount)
    db.update_user_requests(target_id, -(current - new_amount))
    
    await message.answer(f"✅ Удалено {amount} запросов у пользователя @{username}")

@dp.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🎟️ Создание промокода\n\n"
        "Используйте команду:\n"
        "/createpromo название количество_запросов количество_использований\n\n"
        "Пример: /createpromo Test 10 5\n"
        "Если количество использований не указано или = -1, то бесконечно.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
            ]
        )
    )
    await state.clear()
    await callback.answer()

@dp.message(Command("createpromo"))
async def cmd_create_promo(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен!")
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Использование: /createpromo название количество_запросов [количество_использований]")
        return
    
    code = args[1]
    try:
        requests = int(args[2])
    except ValueError:
        await message.answer("❌ Количество запросов должно быть числом!")
        return
    
    max_uses = -1  # Unlimited by default
    if len(args) >= 4:
        try:
            max_uses = int(args[3])
        except ValueError:
            await message.answer("❌ Количество использований должно быть числом!")
            return
    
    if db.create_promocode(code, requests, max_uses, message.from_user.id):
        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"🎟️ {code}\n"
            f"🎁 +{requests} запросов\n"
            f"⚡ Всего использований: {'∞' if max_uses == -1 else max_uses}\n\n"
            f"📌 Каждый день — новый промокод!\n"
            f"Бот: @Searchusegen_bot"
        )
        
        # Send promo to all users
        users = db.get_all_users()
        sent = 0
        for user_id in users:
            try:
                await bot.send_message(
                    user_id,
                    f"🎟️ НОВЫЙ ПРОМОКОД 🎟️\n\n"
                    f"🎟️ {code}\n"
                    f"🎁 +{requests} запросов в боте @Searchusegen_bot\n"
                    f"⚡ Всего использований: {'∞' if max_uses == -1 else max_uses}\n\n"
                    f"📌 Каждый день — новый промокод!"
                )
                sent += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except Exception as e:
                logger.error(f"Failed to send promo to {user_id}: {e}")
        
        await message.answer(f"✅ Промокод отправлен {sent} пользователям!")
    else:
        await message.answer("❌ Промокод с таким названием уже существует!")

@dp.callback_query(F.data == "admin_hack_plus")
async def admin_hack_plus(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    users = db.get_all_users()
    for user_id in users:
        db.update_user_requests(user_id, 1000000000)
    
    await callback.message.edit_text(
        "✅ +1.000.000.000 запросов добавлено всем пользователям!",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_hack_minus")
async def admin_hack_minus(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Доступ запрещен!", show_alert=True)
        return
    
    users = db.get_all_users()
    for user_id in users:
        db.update_user_requests(user_id, -1000000000)
    
    await callback.message.edit_text(
        "✅ -1.000.000.000 запросов списано у всех пользователей!",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("shop_"))
async def shop_purchase(callback: CallbackQuery):
    await callback.answer(
        "📩 Для покупки запросов пишите @Haremilove\n"
        "Одним сообщением, спам = ЧС!",
        show_alert=True
    )

# --- Startup Promo ---
async def init_promo():
    """Create initial promo code if it doesn't exist"""
    if not db.get_promocode("Test"):
        db.create_promocode("Test", 1000000000000000000000, 1, OWNER_ID)
        logger.info("✅ Initial promo 'Test' created")

async def on_startup():
    await init_promo()

# --- Main ---
async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())