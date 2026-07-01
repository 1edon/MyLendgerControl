import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Загрузка конфига
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден! Проверьте файл .env")

DB_PATH = "finance.db"
DEFAULT_TIMEZONE = "Europe/Moscow"

POPULAR_TIMEZONES = [
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Samara",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Novosibirsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Yakutsk",
    "Asia/Vladivostok",
    "Asia/Magadan",
    "Asia/Kamchatka",
]

DEFAULT_INCOME_CATEGORIES = ["Зарплата", "Подарки", "Подработка", "Инвестиции"]
DEFAULT_EXPENSE_CATEGORIES = ["Еда", "Транспорт", "Жилье", "Развлечения", "Здоровье"]

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class TransactionStates(StatesGroup):
    choosing_category = State()
    adding_new_category = State()
    entering_amount = State()
    entering_comment = State()


class CategoryStates(StatesGroup):
    adding_category = State()


class DebtStates(StatesGroup):
    entering_person = State()
    entering_amount = State()
    entering_comment = State()


class SettingsStates(StatesGroup):
    entering_timezone = State()


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                last_bot_message_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income','expense')),
                name TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income','expense')),
                category_id INTEGER,
                amount REAL NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (category_id) REFERENCES categories(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                operation_type TEXT NOT NULL CHECK(operation_type IN (
                    'i_borrowed','i_returned','they_borrowed','they_returned'
                )),
                person_name TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована")


async def db_get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_create_user(user_id: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, timezone, created_at) VALUES (?,?,?)",
            (user_id, DEFAULT_TIMEZONE, now),
        )
        for name in DEFAULT_INCOME_CATEGORIES:
            await db.execute(
                "INSERT INTO categories (user_id, type, name) VALUES (?, 'income', ?)",
                (user_id, name),
            )
        for name in DEFAULT_EXPENSE_CATEGORIES:
            await db.execute(
                "INSERT INTO categories (user_id, type, name) VALUES (?, 'expense', ?)",
                (user_id, name),
            )
        await db.commit()
    return await db_get_user(user_id)


async def db_get_or_create_user(user_id: int) -> dict:
    user = await db_get_user(user_id)
    if not user:
        user = await db_create_user(user_id)
    return user


async def db_update_timezone(user_id: int, tz: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?", (tz, user_id)
        )
        await db.commit()


async def db_save_last_message(user_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_bot_message_id = ? WHERE user_id = ?",
            (message_id, user_id),
        )
        await db.commit()


async def db_get_categories(user_id: int, cat_type: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM categories WHERE user_id=? AND type=? ORDER BY name",
            (user_id, cat_type),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_get_category(cat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM categories WHERE id=?", (cat_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_add_category(user_id: int, cat_type: str, name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO categories (user_id, type, name) VALUES (?,?,?)",
            (user_id, cat_type, name),
        )
        await db.commit()
        return cur.lastrowid


async def db_delete_category(cat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM categories WHERE id=? AND user_id=?", (cat_id, user_id)
        )
        await db.commit()


async def db_add_transaction(
    user_id: int, ttype: str, cat_id: int, amount: float, comment: str | None
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO transactions (user_id,type,category_id,amount,comment,created_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, ttype, cat_id, amount, comment, now),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_total(
    user_id: int, ttype: str, start: datetime, end: datetime
) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COALESCE(SUM(amount),0) FROM transactions
               WHERE user_id=? AND type=? AND created_at>=? AND created_at<?""",
            (user_id, ttype, start.isoformat(), end.isoformat()),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0.0


async def db_get_transactions(
    user_id: int, ttype: str, start: datetime, end: datetime
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT t.*, c.name as category_name
               FROM transactions t
               LEFT JOIN categories c ON t.category_id=c.id
               WHERE t.user_id=? AND t.type=? AND t.created_at>=? AND t.created_at<?
               ORDER BY t.created_at DESC""",
            (user_id, ttype, start.isoformat(), end.isoformat()),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_get_top_categories(
    user_id: int, ttype: str, start: datetime, end: datetime, limit: int = 5
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.name, SUM(t.amount) as total
               FROM transactions t
               LEFT JOIN categories c ON t.category_id=c.id
               WHERE t.user_id=? AND t.type=? AND t.created_at>=? AND t.created_at<?
               GROUP BY t.category_id ORDER BY total DESC LIMIT ?""",
            (user_id, ttype, start.isoformat(), end.isoformat(), limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_add_debt(
    user_id: int, op_type: str, person: str, amount: float, comment: str | None
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO debts (user_id,operation_type,person_name,amount,comment,created_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, op_type, person, amount, comment, now),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_debt_summary(user_id: int) -> dict:
    """
    Считаем балансы:
    i_owe[person]    = сумма i_borrowed - сумма i_returned  (я должен)
    they_owe[person] = сумма they_borrowed - сумма they_returned (мне должны)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            """SELECT person_name,
               SUM(CASE WHEN operation_type='i_borrowed' THEN amount ELSE 0 END) -
               SUM(CASE WHEN operation_type='i_returned' THEN amount ELSE 0 END) as balance
               FROM debts WHERE user_id=? AND operation_type IN ('i_borrowed','i_returned')
               GROUP BY person_name""",
            (user_id,),
        ) as cur:
            i_owe = {r["person_name"]: r["balance"] for r in await cur.fetchall()
                     if r["balance"] > 0}

        async with db.execute(
            """SELECT person_name,
               SUM(CASE WHEN operation_type='they_borrowed' THEN amount ELSE 0 END) -
               SUM(CASE WHEN operation_type='they_returned' THEN amount ELSE 0 END) as balance
               FROM debts WHERE user_id=? AND operation_type IN ('they_borrowed','they_returned')
               GROUP BY person_name""",
            (user_id,),
        ) as cur:
            they_owe = {r["person_name"]: r["balance"] for r in await cur.fetchall()
                        if r["balance"] > 0}

    return {"i_owe": i_owe, "they_owe": they_owe}


async def db_get_debt_history(user_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM debts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def parse_amount(text: str) -> float | None:
    """Парсит сумму: 1000 / 1000.50 / 1000,50"""
    text = text.strip().replace(",", ".").replace(" ", "")
    try:
        val = float(text)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def fmt_amount(amount: float) -> str:
    """1 234 567 ₽ или 1 234 567.50 ₽"""
    if amount == int(amount):
        return f"{int(amount):,} ₽".replace(",", "\u00a0")
    return f"{amount:,.2f} ₽".replace(",", "\u00a0")


def validate_tz(tz_str: str) -> bool:
    try:
        ZoneInfo(tz_str)
        return True
    except (ZoneInfoNotFoundError, KeyError):
        return False


def get_tz(tz_str: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def month_bounds_utc(tz_str: str) -> tuple[datetime, datetime]:
    tz = get_tz(tz_str)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def day_bounds_utc(tz_str: str) -> tuple[datetime, datetime]:
    tz = get_tz(tz_str)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def all_time_bounds() -> tuple[datetime, datetime]:
    return (
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime(2100, 1, 1, tzinfo=timezone.utc),
    )


def fmt_date(dt_str: str, tz_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
        return dt.astimezone(get_tz(tz_str)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str


MONTH_NAMES = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

DEBT_LABELS = {
    "i_borrowed": "Я взял в долг",
    "i_returned": "Я вернул долг",
    "they_borrowed": "У меня взяли в долг",
    "they_returned": "Мне вернули долг",
}


async def safe_delete(bot: Bot, chat_id: int, message_id: int | None):
    """Удаляет сообщение, не падая при ошибке."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.debug(f"safe_delete error: {e}")


async def send_or_edit(
    bot: Bot,
    user: dict,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    state: FSMContext | None = None,
) -> Message:
    """
    Ключевая функция принципа «одно сообщение».
    1. Удаляем старое сообщение бота (если есть).
    2. Отправляем новое.
    3. Сохраняем ID нового сообщения в БД.
    """
    await safe_delete(bot, chat_id, user.get("last_bot_message_id"))
    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    await db_save_last_message(user["user_id"], msg.message_id)
    # Обновляем user в state, чтобы последующие вызовы знали актуальный ID
    if state:
        data = await state.get_data()
        data["last_msg_id"] = msg.message_id
        await state.update_data(data)
    return msg


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def kb_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="💰 Ввести доход", callback_data="add_income"),
        InlineKeyboardButton(text="💸 Ввести расход", callback_data="add_expense"),
    )
    b.row(
        InlineKeyboardButton(text="📂 Категории доходов", callback_data="cats_income"),
        InlineKeyboardButton(text="📂 Категории расходов", callback_data="cats_expense"),
    )
    b.row(InlineKeyboardButton(text="🤝 Долги", callback_data="debts_menu"))
    b.row(InlineKeyboardButton(text="📊 Детальная статистика", callback_data="stats_detail"))
    b.row(InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"))
    return b.as_markup()


def kb_back_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_cancel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()


def kb_categories_select(categories: list[dict], prefix: str) -> InlineKeyboardMarkup:
    """Список категорий для выбора при вводе транзакции."""
    b = InlineKeyboardBuilder()
    for cat in categories:
        b.row(InlineKeyboardButton(
            text=cat["name"], callback_data=f"{prefix}:{cat['id']}"
        ))
    b.row(InlineKeyboardButton(text="➕ Добавить новую категорию", callback_data="add_new_cat"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()


def kb_categories_manage(categories: list[dict], cat_type: str) -> InlineKeyboardMarkup:
    """Список категорий с кнопками удаления."""
    b = InlineKeyboardBuilder()
    for cat in categories:
        b.row(InlineKeyboardButton(
            text=f"🗑 {cat['name']}", callback_data=f"del_cat:{cat['id']}"
        ))
    b.row(InlineKeyboardButton(
        text="➕ Добавить категорию", callback_data=f"new_cat:{cat_type}"
    ))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_comment() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⏭ Без комментария", callback_data="no_comment"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()


def kb_debts_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📥 Я взял в долг", callback_data="debt:i_borrowed"),
        InlineKeyboardButton(text="📤 Я вернул долг", callback_data="debt:i_returned"),
    )
    b.row(
        InlineKeyboardButton(text="📤 У меня взяли", callback_data="debt:they_borrowed"),
        InlineKeyboardButton(text="📥 Мне вернули", callback_data="debt:they_returned"),
    )
    b.row(
        InlineKeyboardButton(text="📋 Текущие долги", callback_data="debt:summary"),
        InlineKeyboardButton(text="📜 История", callback_data="debt:history"),
    )
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_stats_periods() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📅 За день", callback_data="stats:day"),
        InlineKeyboardButton(text="📅 За месяц", callback_data="stats:month"),
        InlineKeyboardButton(text="📅 За всё время", callback_data="stats:all"),
    )
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_timezones() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for tz in POPULAR_TIMEZONES:
        b.row(InlineKeyboardButton(text=tz, callback_data=f"settz:{tz}"))
    b.row(InlineKeyboardButton(
        text="✏️ Ввести вручную", callback_data="settz:manual"
    ))
    b.row(InlineKeyboardButton(text="🏠 В главное меню", callback_data="main_menu"))
    return b.as_markup()


# ---------------------------------------------------------------------------
# Тексты экранов
# ---------------------------------------------------------------------------

async def build_main_menu_text(user_id: int, tz_str: str) -> str:
    start, end = month_bounds_utc(tz_str)
    income = await db_get_total(user_id, "income", start, end)
    expense = await db_get_total(user_id, "expense", start, end)
    balance = income - expense

    tz = get_tz(tz_str)
    now = datetime.now(tz)
    month_name = MONTH_NAMES.get(now.month, "")

    return (
        f"<b>💼 Финансовый учёт</b>\n\n"
        f"📅 Дата: <b>{now.strftime('%d.%m.%Y')}</b>\n"
        f"🌍 Часовой пояс: <b>{tz_str}</b>\n\n"
        f"<b>Статистика за {month_name}:</b>\n"
        f"💰 Доходы:  <b>{fmt_amount(income)}</b>\n"
        f"💸 Расходы: <b>{fmt_amount(expense)}</b>\n"
        f"📊 Баланс:  <b>{fmt_amount(balance)}</b>\n\n"
        f"Выберите действие:"
    )


async def build_stats_text(user_id: int, tz_str: str, period: str) -> str:
    if period == "day":
        start, end = day_bounds_utc(tz_str)
        period_label = "день"
    elif period == "month":
        start, end = month_bounds_utc(tz_str)
        tz = get_tz(tz_str)
        month_name = MONTH_NAMES.get(datetime.now(tz).month, "")
        period_label = month_name
    else:
        start, end = all_time_bounds()
        period_label = "всё время"

    income = await db_get_total(user_id, "income", start, end)
    expense = await db_get_total(user_id, "expense", start, end)
    balance = income - expense

    top_exp = await db_get_top_categories(user_id, "expense", start, end)
    top_inc = await db_get_top_categories(user_id, "income", start, end)

    text = (
        f"<b>📊 Статистика за {period_label}</b>\n\n"
        f"💰 Доходы:  <b>{fmt_amount(income)}</b>\n"
        f"💸 Расходы: <b>{fmt_amount(expense)}</b>\n"
        f"📈 Баланс:  <b>{fmt_amount(balance)}</b>\n"
    )

    if top_exp:
        text += "\n<b>Топ расходов по категориям:</b>\n"
        for i, row in enumerate(top_exp, 1):
            text += f"  {i}. {row['name'] or '—'}: {fmt_amount(row['total'])}\n"

    if top_inc:
        text += "\n<b>Топ доходов по категориям:</b>\n"
        for i, row in enumerate(top_inc, 1):
            text += f"  {i}. {row['name'] or '—'}: {fmt_amount(row['total'])}\n"

    if not top_exp and not top_inc:
        text += "\n<i>Транзакций за этот период нет.</i>"

    return text


# ---------------------------------------------------------------------------
# Роутер и хендлеры
# ---------------------------------------------------------------------------
router = Router()


# ======================== /start ========================

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    user = await db_get_or_create_user(message.from_user.id)

    # Удаляем сообщение пользователя /start
    await safe_delete(bot, message.chat.id, message.message_id)

    text = await build_main_menu_text(user["user_id"], user["timezone"])
    msg = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=kb_main_menu(),
        parse_mode="HTML",
    )
    await db_save_last_message(user["user_id"], msg.message_id)


# ======================== ГЛАВНОЕ МЕНЮ ========================

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    user = await db_get_or_create_user(call.from_user.id)

    text = await build_main_menu_text(user["user_id"], user["timezone"])
    await safe_delete(bot, call.message.chat.id, user.get("last_bot_message_id"))

    msg = await bot.send_message(
        chat_id=call.message.chat.id,
        text=text,
        reply_markup=kb_main_menu(),
        parse_mode="HTML",
    )
    await db_save_last_message(user["user_id"], msg.message_id)
    await call.answer()


# ======================== ТРАНЗАКЦИИ ========================

@router.callback_query(F.data.in_({"add_income", "add_expense"}))
async def cb_add_transaction_start(call: CallbackQuery, bot: Bot, state: FSMContext):
    user = await db_get_or_create_user(call.from_user.id)
    ttype = "income" if call.data == "add_income" else "expense"
    categories = await db_get_categories(user["user_id"], ttype)

    await state.set_state(TransactionStates.choosing_category)
    await state.update_data(ttype=ttype)

    label = "дохода" if ttype == "income
