import asyncio
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sqlalchemy import BigInteger, Float, ForeignKey, String, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("finance_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")

DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///finance.db")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")

DEFAULT_INCOME_CATEGORIES = [
    "Зарплата",
    "Подработка",
    "Подарок",
    "Инвестиции",
    "Прочее",
]
DEFAULT_EXPENSE_CATEGORIES = [
    "Еда",
    "Транспорт",
    "Жильё",
    "Развлечения",
    "Здоровье",
    "Одежда",
    "Прочее",
]

# Список таймзон для быстрого выбора в настройках
TZ_CHOICES = [
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Samara",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Yakutsk",
    "Asia/Vladivostok",
    "Asia/Kamchatka",
    "Europe/Kyiv",
    "Europe/Minsk",
    "Asia/Almaty",
    "Asia/Tashkent",
    "UTC",
]

# --------------------------------------------------------------------------- #
# Модели БД
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram id
    timezone: Mapped[str] = mapped_column(String(64), default=DEFAULT_TZ)
    last_bot_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


class IncomeCategory(Base):
    __tablename__ = "income_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)

    records: Mapped[list["IncomeRecord"]] = relationship(back_populates="category")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)

    records: Mapped[list["ExpenseRecord"]] = relationship(back_populates="category")


class IncomeRecord(Base):
    __tablename__ = "income_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("income_categories.id"))
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )  # хранится в UTC (naive)

    category: Mapped["IncomeCategory"] = relationship(back_populates="records")


class ExpenseRecord(Base):
    __tablename__ = "expense_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    amount: Mapped[float] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )  # хранится в UTC (naive)

    category: Mapped["ExpenseCategory"] = relationship(back_populates="records")


engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# FSM состояния
# --------------------------------------------------------------------------- #
class AddFlow(StatesGroup):
    amount = State()
    comment = State()


# --------------------------------------------------------------------------- #
# Инициализация БД
# --------------------------------------------------------------------------- #
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        # Дефолтные категории доходов
        existing_income = (await session.execute(select(IncomeCategory))).scalars().all()
        if not existing_income:
            session.add_all([IncomeCategory(name=n) for n in DEFAULT_INCOME_CATEGORIES])
        # Дефолтные категории расходов
        existing_expense = (await session.execute(select(ExpenseCategory))).scalars().all()
        if not existing_expense:
            session.add_all([ExpenseCategory(name=n) for n in DEFAULT_EXPENSE_CATEGORIES])
        await session.commit()
    logger.info("База данных инициализирована")


async def get_or_create_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, timezone=DEFAULT_TZ)
        session.add(user)
        await session.commit()
        logger.info("Создан новый пользователь: %s", user_id)
    return user


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def get_user_tz(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def month_start_utc(user: User) -> datetime:
    """Начало текущего месяца в таймзоне пользователя, приведённое к naive UTC."""
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


async def month_totals(session: AsyncSession, user: User) -> tuple[float, float]:
    start = month_start_utc(user)
    income = (
        await session.execute(
            select(func.coalesce(func.sum(IncomeRecord.amount), 0.0)).where(
                IncomeRecord.user_id == user.id,
                IncomeRecord.created_at >= start,
            )
        )
    ).scalar_one()
    expense = (
        await session.execute(
            select(func.coalesce(func.sum(ExpenseRecord.amount), 0.0)).where(
                ExpenseRecord.user_id == user.id,
                ExpenseRecord.created_at >= start,
            )
        )
    ).scalar_one()
    return float(income), float(expense)


# --------------------------------------------------------------------------- #
# Единое актуальное сообщение
# --------------------------------------------------------------------------- #
async def send_or_edit(
    bot: Bot,
    session: AsyncSession,
    user: User,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Держим в чате одно актуальное сообщение: сначала пробуем редактировать,
    если нельзя — удаляем старое и отправляем новое."""
    chat_id = user.id
    if user.last_bot_message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=user.last_bot_message_id,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            try:
                await bot.delete_message(chat_id, user.last_bot_message_id)
            except TelegramBadRequest:
                pass

    msg = await bot.send_message(chat_id, text, reply_markup=keyboard)
    user.last_bot_message_id = msg.message_id
    await session.commit()


# --------------------------------------------------------------------------- #
# Клавиатуры
# --------------------------------------------------------------------------- #
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Доход", callback_data="add:income"),
                InlineKeyboardButton(text="➖ Расход", callback_data="add:expense"),
            ],
            [InlineKeyboardButton(text="📊 Статистика за месяц", callback_data="stats")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        ]
    )


def categories_kb(kind: str, categories) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for cat in categories:
        row.append(
            InlineKeyboardButton(text=cat.name, callback_data=f"cat:{kind}:{cat.id}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:main")]]
    )


def comment_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_comment")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:main")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]]
    )


def settings_kb(current_tz: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for tz in TZ_CHOICES:
        mark = "✅ " if tz == current_tz else ""
        row.append(InlineKeyboardButton(text=f"{mark}{tz}", callback_data=f"tz:{tz}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Тексты экранов
# --------------------------------------------------------------------------- #
async def build_main_menu_text(session: AsyncSession, user: User) -> str:
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    income, expense = await month_totals(session, user)
    balance = income - expense
    return (
        "<b>💰 Личные финансы</b>\n\n"
        f"📅 Дата: <b>{now_local.strftime('%d.%m.%Y %H:%M')}</b>\n"
        f"🌍 Таймзона: <code>{user.timezone}</code>\n\n"
        f"📈 Доходы за месяц: <b>{fmt_money(income)}</b>\n"
        f"📉 Расходы за месяц: <b>{fmt_money(expense)}</b>\n"
        f"🧮 Баланс: <b>{fmt_money(balance)}</b>\n\n"
        "Выберите действие:"
    )


async def build_stats_text(session: AsyncSession, user: User) -> str:
    tz = get_user_tz(user)
    now_local = datetime.now(tz)
    start = month_start_utc(user)

    income_rows = (
        await session.execute(
            select(IncomeCategory.name, func.sum(IncomeRecord.amount))
            .join(IncomeRecord, IncomeRecord.category_id == IncomeCategory.id)
            .where(IncomeRecord.user_id == user.id, IncomeRecord.created_at >= start)
            .group_by(IncomeCategory.name)
            .order_by(func.sum(IncomeRecord.amount).desc())
        )
    ).all()

    expense_rows = (
        await session.execute(
            select(ExpenseCategory.name, func.sum(ExpenseRecord.amount))
            .join(ExpenseRecord, ExpenseRecord.category_id == ExpenseCategory.id)
            .where(ExpenseRecord.user_id == user.id, ExpenseRecord.created_at >= start)
            .group_by(ExpenseCategory.name)
            .order_by(func.sum(ExpenseRecord.amount).desc())
        )
    ).all()

    income_total = sum(v for _, v in income_rows)
    expense_total = sum(v for _, v in expense_rows)

    lines = [f"<b>📊 Статистика за {now_local.strftime('%B %Y')}</b>\n"]

    lines.append("<b>📈 Доходы:</b>")
    if income_rows:
        for name, value in income_rows:
            lines.append(f"  • {name}: {fmt_money(float(value))}")
    else:
        lines.append("  — нет записей")
    lines.append(f"  <b>Итого: {fmt_money(float(income_total))}</b>\n")

    lines.append("<b>📉 Расходы:</b>")
    if expense_rows:
        for name, value in expense_rows:
            lines.append(f"  • {name}: {fmt_money(float(value))}")
    else:
        lines.append("  — нет записей")
    lines.append(f"  <b>Итого: {fmt_money(float(expense_total))}</b>\n")

    lines.append(f"🧮 <b>Баланс: {fmt_money(float(income_total - expense_total))}</b>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Хэндлеры
# --------------------------------------------------------------------------- #
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        # чистим сообщение пользователя
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        text = await build_main_menu_text(session, user)
        await send_or_edit(bot, session, user, text, main_menu_kb())


@dp.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        text = await build_main_menu_text(session, user)
        await send_or_edit(bot, session, user, text, main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "stats")
async def cb_stats(callback: CallbackQuery, bot: Bot) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        text = await build_stats_text(session, user)
        await send_or_edit(bot, session, user, text, back_kb())
    await callback.answer()


@dp.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery, bot: Bot) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        text = (
            "<b>⚙️ Настройки</b>\n\n"
            f"Текущая таймзона: <code>{user.timezone}</code>\n\n"
            "Выберите таймзону из списка:"
        )
        await send_or_edit(bot, session, user, text, settings_kb(user.timezone))
    await callback.answer()


@dp.callback_query(F.data.startswith("tz:"))
async def cb_set_tz(callback: CallbackQuery, bot: Bot) -> None:
    tz_name = callback.data.split(":", 1)[1]
    if tz_name not in available_timezones():
        await callback.answer("Неизвестная таймзона", show_alert=True)
        return
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        user.timezone = tz_name
        await session.commit()
        text = (
            "<b>⚙️ Настройки</b>\n\n"
            f"Таймзона обновлена: <code>{user.timezone}</code>\n\n"
            "Выберите таймзону из списка:"
        )
        await send_or_edit(bot, session, user, text, settings_kb(user.timezone))
    await callback.answer("Таймзона сохранена ✅")


@dp.callback_query(F.data.in_({"add:income", "add:expense"}))
async def cb_add_start(callback: CallbackQuery, bot: Bot) -> None:
    kind = callback.data.split(":", 1)[1]  # income / expense
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        if kind == "income":
            cats = (await session.execute(select(IncomeCategory).order_by(IncomeCategory.id))).scalars().all()
            title = "➕ Добавление дохода"
        else:
            cats = (await session.execute(select(ExpenseCategory).order_by(ExpenseCategory.id))).scalars().all()
            title = "➖ Добавление расхода"
        text = f"<b>{title}</b>\n\nВыберите категорию:"
        await send_or_edit(bot, session, user, text, categories_kb(kind, cats))
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cb_choose_category(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, kind, cat_id = callback.data.split(":")
    cat_id = int(cat_id)

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        if kind == "income":
            cat = await session.get(IncomeCategory, cat_id)
        else:
            cat = await session.get(ExpenseCategory, cat_id)
        if cat is None:
            await callback.answer("Категория не найдена", show_alert=True)
            return

        await state.update_data(kind=kind, category_id=cat_id, category_name=cat.name)
        await state.set_state(AddFlow.amount)

        title = "➕ Доход" if kind == "income" else "➖ Расход"
        text = (
            f"<b>{title}</b>\n"
            f"Категория: <b>{cat.name}</b>\n\n"
            "Введите сумму (например: <code>1500</code> или <code>99.90</code>):"
        )
        await send_or_edit(bot, session, user, text, cancel_kb())
    await callback.answer()


@dp.message(AddFlow.amount)
async def process_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            data = await state.get_data()
            title = "➕ Доход" if data["kind"] == "income" else "➖ Расход"
            text = (
                f"<b>{title}</b>\n"
                f"Категория: <b>{data['category_name']}</b>\n\n"
                "⚠️ Некорректная сумма. Введите положительное число, "
                "например <code>1500</code> или <code>99.90</code>:"
            )
            await send_or_edit(bot, session, user, text, cancel_kb())
            return

        await state.update_data(amount=amount)
        await state.set_state(AddFlow.comment)

        data = await state.get_data()
        title = "➕ Доход" if data["kind"] == "income" else "➖ Расход"
        text = (
            f"<b>{title}</b>\n"
            f"Категория: <b>{data['category_name']}</b>\n"
            f"Сумма: <b>{fmt_money(amount)}</b>\n\n"
            "Добавьте комментарий или нажмите «Пропустить»:"
        )
        await send_or_edit(bot, session, user, text, comment_kb())


async def _save_record(session: AsyncSession, user: User, data: dict, comment: str | None) -> None:
    if data["kind"] == "income":
        record = IncomeRecord(
            user_id=user.id,
            category_id=data["category_id"],
            amount=data["amount"],
            comment=comment,
        )
    else:
        record = ExpenseRecord(
            user_id=user.id,
            category_id=data["category_id"],
            amount=data["amount"],
            comment=comment,
        )
    session.add(record)
    await session.commit()


@dp.message(AddFlow.comment)
async def process_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    comment = (message.text or "").strip() or None
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        data = await state.get_data()
        await _save_record(session, user, data, comment)
        await state.clear()
        text = await build_main_menu_text(session, user)
        await send_or_edit(bot, session, user, "✅ Запись сохранена!\n\n" + text, main_menu_kb())


@dp.callback_query(AddFlow.comment, F.data == "skip_comment")
async def cb_skip_comment(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        data = await state.get_data()
        await _save_record(session, user, data, None)
        await state.clear()
        text = await build_main_menu_text(session, user)
        await send_or_edit(bot, session, user, "✅ Запись сохранена!\n\n" + text, main_menu_kb())
    await callback.answer("Сохранено ✅")


@dp.message()
async def fallback_message(message: Message, bot: Bot) -> None:
    """Любое сообщение вне сценария — показываем главное меню и чистим ввод."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        text = await build_main_menu_text(session, user)
        await send_or_edit(bot, session, user, text, main_menu_kb())


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #
async def main() -> None:
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    logger.info("Бот запускается (long polling)...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Выход по сигналу пользователя")