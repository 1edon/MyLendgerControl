import os
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, html
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.client.default import DefaultBotProperties
import aiosqlite

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = os.getenv("DB_NAME", "finance_bot.db")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =====================================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# =====================================================================
async def init_db():
    """Инициализация базы данных SQLite"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        # Таблица категорий расходов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        # Таблица доходов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL NOT NULL,
                comment TEXT,
                date TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        # Таблица расходов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                category_id INTEGER,
                amount REAL NOT NULL,
                comment TEXT,
                date TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        """)
        # Таблица долгов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                debtor_creditor TEXT NOT NULL,
                amount REAL NOT NULL,
                debt_type TEXT NOT NULL, 
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована")

async def seed_default_categories(user_id: int):
    """Создание категорий по умолчанию для нового пользователя"""
    default_categories = ["Продукты", "Транспорт", "Кафе"]
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", (user_id,)) as cursor:
            count = await cursor.fetchone()
            if count[0] == 0:
                for cat in default_categories:
                    await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, cat))
                await db.commit()
                logger.info(f"Созданы категории по умолчанию для пользователя {user_id}")

# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ УДАЛЕНИЯ СООБЩЕНИЙ
# =====================================================================
async def delete_previous_messages(message: Message, state: FSMContext):
    """Удаляет все предыдущие сообщения пользователя в чате"""
    data = await state.get_data()
    message_ids = data.get("message_ids", [])
    
    if message_ids:
        try:
            for msg_id in message_ids:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
                except Exception as e:
                    # Игнорируем ошибки при удалении (сообщение уже могло быть удалено)
                    pass
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщений: {e}")
    
    # Сохраняем ID текущего сообщения
    await state.update_data(message_ids=[message.message_id])

async def add_message_to_cleanup(message: Message, state: FSMContext):
    """Добавляет сообщение в список для последующей очистки"""
    data = await state.get_data()
    message_ids = data.get("message_ids", [])
    message_ids.append(message.message_id)
    await state.update_data(message_ids=message_ids)

# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def get_main_menu():
    """Главное меню бота (инлайн)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Доходы", callback_data="menu_income")],
        [InlineKeyboardButton(text="📉 Расходы", callback_data="menu_expense")],
        [InlineKeyboardButton(text="🗂 Категории", callback_data="menu_categories")],
        [InlineKeyboardButton(text="🤝 Долги", callback_data="menu_debts")],
        [InlineKeyboardButton(text="📊 Статистика за месяц", callback_data="menu_statistics")]
    ])

def get_back_menu():
    """Кнопки навигации: Назад и В меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
            InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")
        ]
    ])

def get_categories_menu():
    """Меню управления категориями"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="cat_add")],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data="cat_del_list")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
         InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")]
    ])

def get_debts_menu():
    """Меню управления долгами"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋‍♂️ Мне должны (Новый)", callback_data="debt_owed_to_me"),
         InlineKeyboardButton(text="💰 Мне вернули", callback_data="return_owed_to_me")],
        [InlineKeyboardButton(text="🙇‍♂️ Я должен (Новый)", callback_data="debt_i_owe"),
         InlineKeyboardButton(text="💸 Я вернул", callback_data="return_i_owe")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
         InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")]
    ])

# =====================================================================
# СТАТУСЫ FSM
# =====================================================================
class FinanceStates(StatesGroup):
    """Состояния конечного автомата"""
    # Доходы
    income_amount = State()
    income_comment = State()
    # Расходы
    expense_category = State()
    expense_amount = State()
    expense_comment = State()
    # Категории
    add_category = State()
    delete_category = State()
    # Долги
    debt_name = State()
    debt_amount = State()
    debt_return_select = State()
    debt_return_amount = State()

# =====================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =====================================================================
storage = MemoryStorage()
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher(storage=storage)

# =====================================================================
# ХЕНДЛЕРЫ
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()
    
    # Регистрируем пользователя
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
    await seed_default_categories(message.from_user.id)
    
    # Очищаем историю сообщений
    await state.update_data(message_ids=[])
    
    # Отправляем главное меню
    await message.answer(
        f"Привет, {html.bold(message.from_user.full_name)}! 👋\n\n"
        "Я твой бот-финансист. Выбери действие в меню ниже:",
        reply_markup=get_main_menu()
    )
    logger.info(f"Пользователь {message.from_user.id} запустил бота")

# ---------------------------------------------------------------------
# ОБРАБОТЧИКИ ГЛАВНОГО МЕНЮ
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "🏠 Главное меню:\n\nВыберите действие:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "back")
async def go_back(callback: CallbackQuery, state: FSMContext):
    """Возврат на предыдущий шаг"""
    await callback.message.delete()
    await callback.message.answer(
        "Выберите действие:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

# ---------------------------------------------------------------------
# СЦЕНАРИЙ: ДОХОДЫ
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "menu_income")
async def process_income_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(FinanceStates.income_amount)
    msg = await callback.message.answer(
        "💰 Введите сумму дохода:",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.message(FinanceStates.income_amount)
async def process_income_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(FinanceStates.income_comment)
        
        # Удаляем сообщение с суммой
        await message.delete()
        
        msg = await message.answer(
            "Введите комментарий (или '-' если нет):",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
    except ValueError:
        msg = await message.answer(
            "❌ Пожалуйста, введите корректное положительное число.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)

@dp.message(FinanceStates.income_comment)
async def process_income_comment(message: Message, state: FSMContext):
    comment = message.text if message.text != "-" else ""
    data = await state.get_data()
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # Сохраняем доход
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO incomes (user_id, amount, comment, date) VALUES (?, ?, ?, ?)",
            (message.from_user.id, data['amount'], comment, date_str)
        )
        await db.commit()
    
    # Удаляем все сообщения
    await delete_previous_messages(message, state)
    await message.delete()
    
    # Отправляем подтверждение и возвращаем в главное меню
    await message.answer(
        f"✅ Доход сохранен: {data['amount']} руб.\n\n"
        "Что хотите сделать дальше?",
        reply_markup=get_main_menu()
    )
    await state.clear()

# ---------------------------------------------------------------------
# СЦЕНАРИЙ: РАСХОДЫ
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "menu_expense")
async def process_expense_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            categories = await cursor.fetchall()
            
    if not categories:
        msg = await callback.message.answer(
            "❌ У вас нет категорий. Сначала добавьте их в меню '🗂 Категории'.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
        await callback.answer()
        return

    buttons = [[InlineKeyboardButton(text=cat[1], callback_data=f"exp_cat_{cat[0]}")] for cat in categories]
    buttons.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await state.set_state(FinanceStates.expense_category)
    msg = await callback.message.answer(
        "📉 Выберите категорию расхода:",
        reply_markup=kb
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.callback_query(FinanceStates.expense_category, F.data.startswith("exp_cat_"))
async def process_expense_cat_chosen(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[2])
    await state.update_data(category_id=cat_id)
    await state.set_state(FinanceStates.expense_amount)
    
    await callback.message.delete()
    msg = await callback.message.answer(
        "Введите сумму расхода:",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.message(FinanceStates.expense_amount)
async def process_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(FinanceStates.expense_comment)
        
        await message.delete()
        msg = await message.answer(
            "Введите комментарий (или '-' если нет):",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
    except ValueError:
        msg = await message.answer(
            "❌ Пожалуйста, введите корректное положительное число.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)

@dp.message(FinanceStates.expense_comment)
async def process_expense_comment(message: Message, state: FSMContext):
    comment = message.text if message.text != "-" else ""
    data = await state.get_data()
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # Сохраняем расход
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO expenses (user_id, category_id, amount, comment, date) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, data['category_id'], data['amount'], comment, date_str)
        )
        await db.commit()
    
    # Удаляем все сообщения
    await delete_previous_messages(message, state)
    await message.delete()
    
    # Отправляем подтверждение и возвращаем в главное меню
    await message.answer(
        f"✅ Расход сохранен: {data['amount']} руб.\n\n"
        "Что хотите сделать дальше?",
        reply_markup=get_main_menu()
    )
    await state.clear()

# ---------------------------------------------------------------------
# СЦЕНАРИЙ: КАТЕГОРИИ
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "menu_categories")
async def process_categories_main(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name FROM categories WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            rows = await cursor.fetchall()
    
    cats_str = "\n".join([f"• {r[0]}" for r in rows]) if rows else "Список пуст"
    
    msg = await callback.message.answer(
        f"🗂 Ваши категории расходов:\n\n{cats_str}",
        reply_markup=get_categories_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.callback_query(F.data == "cat_add")
async def cb_add_category(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(FinanceStates.add_category)
    msg = await callback.message.answer(
        "➕ Введите название новой категории:",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.message(FinanceStates.add_category)
async def process_add_category_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        msg = await message.answer(
            "❌ Название не может быть пустым.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
        return
        
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (message.from_user.id, title))
        await db.commit()
    
    # Удаляем все сообщения
    await delete_previous_messages(message, state)
    await message.delete()
    
    await message.answer(
        f"✅ Категория '{title}' успешно добавлена!\n\n"
        "Что хотите сделать дальше?",
        reply_markup=get_main_menu()
    )
    await state.clear()

@dp.callback_query(F.data == "cat_del_list")
async def cb_delete_category_list(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            categories = await cursor.fetchall()
            
    if not categories:
        msg = await callback.message.answer(
            "❌ У вас нет категорий для удаления.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
        await callback.answer()
        return

    buttons = [[InlineKeyboardButton(text=f"❌ {cat[1]}", callback_data=f"cat_del_id_{cat[0]}")] for cat in categories]
    buttons.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await state.set_state(FinanceStates.delete_category)
    msg = await callback.message.answer(
        "⚠️ Выберите категорию для удаления:\n"
        "(Связанные расходы также будут удалены!)",
        reply_markup=kb
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.callback_query(FinanceStates.delete_category, F.data.startswith("cat_del_id_"))
async def cb_delete_category_confirm(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[3])
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (cat_id, callback.from_user.id))
        await db.commit()
    
    await callback.message.delete()
    await callback.message.answer(
        "🗑 Категория успешно удалена.\n\n"
        "Что хотите сделать дальше?",
        reply_markup=get_main_menu()
    )
    await state.clear()
    await callback.answer()

# ---------------------------------------------------------------------
# СЦЕНАРИЙ: ДОЛГИ
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "menu_debts")
async def process_debts_main(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT debtor_creditor, amount, debt_type FROM debts WHERE user_id = ?", (callback.from_user.id,)) as cursor:
            rows = await cursor.fetchall()
            
    owed_to_me = []  # Мне должны
    i_owe = []       # Я должен
    total_owed_to_me = 0
    total_i_owe = 0
    
    for name, amount, d_type in rows:
        if d_type == 'owed_to_me':
            owed_to_me.append(f"• {name}: {amount} руб.")
            total_owed_to_me += amount
        else:
            i_owe.append(f"• {name}: {amount} руб.")
            total_i_owe += amount
    
    total_debts = total_owed_to_me + total_i_owe
            
    text = f"📋 {html.bold('Текущие долги')}\n\n"
    text += f"💰 {html.bold('Общая сумма долгов:')} {total_debts} руб.\n\n"
    
    text += html.underline("🙋‍♂️ Мне должны:\n")
    text += ("\n".join(owed_to_me) if owed_to_me else "Нет записей")
    text += f"\n{html.bold('Итого:')} {total_owed_to_me} руб.\n\n"
    
    text += html.underline("🙇‍♂️ Я должен:\n")
    text += ("\n".join(i_owe) if i_owe else "Нет записей")
    text += f"\n{html.bold('Итого:')} {total_i_owe} руб."
    
    msg = await callback.message.answer(
        text,
        reply_markup=get_debts_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.callback_query(F.data.startswith("debt_"))
async def cb_new_debt(callback: CallbackQuery, state: FSMContext):
    debt_type = callback.data.replace("debt_", "")
    await state.update_data(debt_type=debt_type)
    await state.set_state(FinanceStates.debt_name)
    
    await callback.message.delete()
    msg = await callback.message.answer(
        "👤 Введите имя человека:",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.message(FinanceStates.debt_name)
async def process_debt_name(message: Message, state: FSMContext):
    await state.update_data(debtor_creditor=message.text.strip())
    await state.set_state(FinanceStates.debt_amount)
    
    await message.delete()
    msg = await message.answer(
        "💰 Введите сумму:",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)

@dp.message(FinanceStates.debt_amount)
async def process_debt_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO debts (user_id, debtor_creditor, amount, debt_type) VALUES (?, ?, ?, ?)",
                (message.from_user.id, data['debtor_creditor'], amount, data['debt_type'])
            )
            await db.commit()
        
        # Удаляем все сообщения
        await delete_previous_messages(message, state)
        await message.delete()
        
        await message.answer(
            "✅ Долг успешно зафиксирован!\n\n"
            "Что хотите сделать дальше?",
            reply_markup=get_main_menu()
        )
        await state.clear()
    except ValueError:
        msg = await message.answer(
            "❌ Пожалуйста, введите корректное число.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)

# Погашение долгов
@dp.callback_query(F.data.startswith("return_"))
async def cb_return_debt_list(callback: CallbackQuery, state: FSMContext):
    debt_type = callback.data.replace("return_", "")
    await callback.message.delete()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, debtor_creditor, amount FROM debts WHERE user_id = ? AND debt_type = ?", 
            (callback.from_user.id, debt_type)
        ) as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        msg = await callback.message.answer(
            "❌ Нет подходящих активных долгов.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)
        await callback.answer()
        return
        
    buttons = [[InlineKeyboardButton(text=f"{r[1]} ({r[2]}р)", callback_data=f"ret_sel_{r[0]}")] for r in rows]
    buttons.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="back"),
        InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await state.set_state(FinanceStates.debt_return_select)
    msg = await callback.message.answer(
        "Выберите долг для погашения:",
        reply_markup=kb
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.callback_query(FinanceStates.debt_return_select, F.data.startswith("ret_sel_"))
async def cb_return_debt_amount_request(callback: CallbackQuery, state: FSMContext):
    debt_id = int(callback.data.split("_")[2])
    await state.update_data(debt_id=debt_id)
    await state.set_state(FinanceStates.debt_return_amount)
    
    await callback.message.delete()
    msg = await callback.message.answer(
        "💰 Какую сумму внести (вернуть)?",
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

@dp.message(FinanceStates.debt_return_amount)
async def process_debt_return_final(message: Message, state: FSMContext):
    try:
        return_amount = float(message.text.replace(",", "."))
        if return_amount <= 0:
            raise ValueError
            
        data = await state.get_data()
        debt_id = data['debt_id']
        
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT amount, debtor_creditor FROM debts WHERE id = ?", (debt_id,)) as cursor:
                row = await cursor.fetchone()
                
            if not row:
                msg = await message.answer(
                    "❌ Ошибка: Долг не найден.",
                    reply_markup=get_back_menu()
                )
                await add_message_to_cleanup(msg, state)
                await state.clear()
                return
                
            current_amount, name = row
            new_amount = current_amount - return_amount
            
            if new_amount <= 0:
                await db.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
                await delete_previous_messages(message, state)
                await message.delete()
                await message.answer(
                    f"🎉 Долг перед/от {name} полностью закрыт!\n\n"
                    "Что хотите сделать дальше?",
                    reply_markup=get_main_menu()
                )
            else:
                await db.execute("UPDATE debts SET amount = ? WHERE id = ?", (new_amount, debt_id))
                await delete_previous_messages(message, state)
                await message.delete()
                await message.answer(
                    f"✅ Долг частично погашен.\n"
                    f"Остаток: {new_amount} руб.\n\n"
                    "Что хотите сделать дальше?",
                    reply_markup=get_main_menu()
                )
                
            await db.commit()
        await state.clear()
    except ValueError:
        msg = await message.answer(
            "❌ Пожалуйста, введите корректное положительное число.",
            reply_markup=get_back_menu()
        )
        await add_message_to_cleanup(msg, state)

# ---------------------------------------------------------------------
# СЦЕНАРИЙ: СТАТИСТИКА
# ---------------------------------------------------------------------
@dp.callback_query(F.data == "menu_statistics")
async def process_statistics(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    
    current_month = datetime.now().strftime("%Y-%m")
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Всего доходов за месяц
        async with db.execute(
            "SELECT SUM(amount) FROM incomes WHERE user_id = ? AND strftime('%Y-%m', date) = ?",
            (callback.from_user.id, current_month)
        ) as cursor:
            total_income = (await cursor.fetchone())[0] or 0.0

        # Всего расходов за месяц
        async with db.execute(
            "SELECT SUM(amount) FROM expenses WHERE user_id = ? AND strftime('%Y-%m', date) = ?",
            (callback.from_user.id, current_month)
        ) as cursor:
            total_expense = (await cursor.fetchone())[0] or 0.0
            
        # ТОП-3 категории расходов
        async with db.execute("""
            SELECT c.name, SUM(e.amount) as total 
            FROM expenses e
            JOIN categories c ON e.category_id = c.id
            WHERE e.user_id = ? AND strftime('%Y-%m', e.date) = ?
            GROUP BY e.category_id
            ORDER BY total DESC
            LIMIT 3
        """, (callback.from_user.id, current_month)) as cursor:
            top_categories = await cursor.fetchall()

    balance = total_income - total_expense
    
    text = f"📊 {html.bold('Статистика за текущий месяц')} ({current_month}):\n\n"
    text += f"💰 {html.bold('Доходы:')} {total_income} руб.\n"
    text += f"📉 {html.bold('Расходы:')} {total_expense} руб.\n"
    text += f"⚖️ {html.bold('Баланс:')} {balance} руб.\n\n"
    
    text += html.bold("🔝 ТОП-3 категорий расходов:\n")
    if top_categories:
        for i, (cat_name, sum_val) in enumerate(top_categories, 1):
            text += f"{i}. {cat_name} — {sum_val} руб.\n"
    else:
        text += "За этот месяц расходов еще не было."
    
    msg = await callback.message.answer(
        text,
        reply_markup=get_back_menu()
    )
    await add_message_to_cleanup(msg, state)
    await callback.answer()

# =====================================================================
# ЗАПУСК БОТА
# =====================================================================
async def main():
    """Главная функция запуска бота"""
    if not BOT_TOKEN:
        logger.error("Токен бота не задан в переменной BOT_TOKEN в файле .env")
        return
        
    await init_db()
    
    logger.info("🚀 Бот успешно запущен и готов к работе!")
    logger.info("📊 Нажмите Ctrl+C для остановки")
    
    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")