import asyncio
import logging
import os
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db
from calculator import REGIMES, calculate_price, money
from tariffs import TARIFFS, OIL_VOLUMES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# --- Настройки из переменных окружения ---
TOKEN = os.getenv("BOT_TOKEN") or "8883929833:AAGlagWxgvb-u9AqcXgubfPtWnDpJfOAp6M"

# Исправлена передача ID администратора
ADMIN_ID = int(os.getenv("ADMIN_ID", "1889997265"))

PRICE = int(os.getenv("PRICE", "3000"))     # цена подписки на 1 месяц, ₸

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db.init_db()


class CalcState(StatesGroup):
    category = State()
    volume = State()
    purchase_price = State()


class Setup(StatesGroup):  # первичная настройка
    regime = State()
    margin = State()
    packaging = State()
    delivery = State()


class Edit(StatesGroup):  # изменение одного параметра
    value = State()


class ReportState(StatesGroup):
    text = State()


# --- Вспомогательное ---
def parse_number(text):
    raw = (text or "").strip().replace(" ", "").replace(",", ".").rstrip("%")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


FIELD_PROMPTS = {
    "margin": "Введите желаемую чистую маржу в % от цены продажи, после налогов (например 20):",
    "packaging": "Введите расходы на упаковку одной единицы товара в ₸ (например 100):",
    "delivery": "Введите расходы на доставку одной единицы товара в ₸ (например 100):",
}
FIELD_COLUMN = {"margin": "margin_pct", "packaging": "packaging", "delivery": "delivery"}


def validate(field: str, value):
    """Возвращает текст ошибки или None, если значение подходит."""
    if value is None:
        return "Введите число, например 20 или 12.5"
    if field == "margin":
        if not (Decimal("0") < value < Decimal("100")):
            return "Маржа должна быть больше 0 и меньше 100 (%)."
    else:
        if value < 0 or value > Decimal("10000000"):
            return "Введите сумму в ₸ от 0, например 100."
    return None


def settings_text(s: dict) -> str:
    return (
        "⚙️ Ваши настройки\n\n"
        f"Режим: {REGIMES[s['regime']]['title']}\n"
        f"Чистая маржа: {fmt(s['margin_pct'])}%\n"
        f"Упаковка: {fmt(s['packaging'])} ₸ за единицу\n"
        f"Доставка: {fmt(s['delivery'])} ₸ за единицу\n\n"
        "Что изменить?"
    )


# --- Клавиатуры ---
def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True,
    )


def categories_keyboard():
    names = list(TARIFFS.keys())
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append(["❌ Отмена"])
    return kb(rows)


def volume_keyboard():
    return kb([[x for x in OIL_VOLUMES[:2]], [x for x in OIL_VOLUMES[2:]], ["↩️ Назад", "❌ Отмена"]])


def main_keyboard():
    return kb([["🧮 Новый расчёт"], ["⚙️ Мои настройки", "💳 Подписка"], ["🐞 Сообщить об ошибке"]])


def regime_inline(mode: str):
    """mode: 'ob' (первичная настройка) или 'ed' (изменение в настройках)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=r["title"], callback_data=f"reg:{mode}:{key}")]
            for key, r in REGIMES.items()
        ]
    )


def settings_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Режим", callback_data="edit:regime"),
                InlineKeyboardButton(text="Маржа", callback_data="edit:margin"),
            ],
            [
                InlineKeyboardButton(text="Упаковка", callback_data="edit:packaging"),
                InlineKeyboardButton(text="Доставка", callback_data="edit:delivery"),
            ],
        ]
    )


def buy_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"🛒 Купить на 1 месяц — {PRICE} ₸", callback_data="buy")]]
    )


def confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, согласен", callback_data="buy_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel"),
        ]]
    )


def paid_keyboard(req_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{req_id}")]]
    )


def manager_keyboard(req_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"approve:{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{req_id}"),
        ]]
    )


# --- Доступ ---
def offer_text() -> str:
    return (
        f"Подписка на 1 месяц — {PRICE} ₸.\n\n"
        "Нажмите «Купить»: менеджер пришлёт QR для оплаты. "
        "Как только оплата будет подтверждена, доступ откроется.\n\n"
        "⚠️ Возврат денег не предусмотрен."
    )


REMIND_DAYS = 3                              # за сколько дней напоминать об окончании
KZ_TZ = timezone(timedelta(hours=5))         # Казахстан, UTC+5


def fmt_date(dt) -> str:
    return dt.astimezone(KZ_TZ).strftime("%d.%m.%Y")


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def status_text(user_id: int) -> str:
    if is_admin(user_id):
        return "👑 Админ: доступ без ограничений."
    s = db.get_status(user_id)
    if s["active"]:
        return f"✅ Подписка активна до {fmt_date(s['until'])}."
    return f"Бесплатных расчётов осталось: {s['free_left']} из {db.FREE_LIMIT}."


async def ensure_access(message: Message, state: FSMContext) -> bool:
    if is_admin(message.from_user.id) or db.has_access(message.from_user.id):
        return True
    await state.clear()
    await message.answer(
        "🔒 Бесплатные расчёты закончились.\n\n" + offer_text(),
        reply_markup=buy_keyboard(),
    )
    return False


async def start_setup(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Setup.regime)
    await message.answer(
        "👋 Давайте настроим калькулятор под ваш бизнес. Это нужно один раз, "
        "потом всё можно изменить в «⚙️ Мои настройки».\n\n"
        "1/4. Выберите форму и налоговый режим:",
        reply_markup=regime_inline("ob"),
    )


async def begin_calc(message: Message, state: FSMContext):
    if not db.get_settings(message.from_user.id):
        await start_setup(message, state)
        return
    if not await ensure_access(message, state):
        return
    await state.set_state(CalcState.category)
    await message.answer(
        f"🚗 Калькулятор цены Kaspi\n{status_text(message.from_user.id)}\n\nВыбери категорию товара:",
        reply_markup=categories_keyboard(),
    )


# --- Основное меню ---
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await begin_calc(message, state)


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_keyboard())


@dp.message(F.text == "🧮 Новый расчёт")
async def new_calc(message: Message, state: FSMContext):
    await state.clear()
    await begin_calc(message, state)


@dp.message(F.text == "💳 Подписка")
@dp.message(Command("buy"))
async def subscription(message: Message):
    text = status_text(message.from_user.id) + "\n\n" + offer_text()
    if db.get_status(message.from_user.id)["active"]:
        text += "\n\nМожно продлить заранее: новый месяц добавится к остатку."
    await message.answer(text, reply_markup=buy_keyboard())


# --- Настройки: просмотр и изменение (доступно и без подписки) ---
@dp.message(F.text == "⚙️ Мои настройки")
@dp.message(Command("settings"))
async def my_settings(message: Message, state: FSMContext):
    await state.clear()
    s = db.get_settings(message.from_user.id)
    if not s:
        await start_setup(message, state)
        return
    await message.answer(settings_text(s), reply_markup=settings_inline())


@dp.callback_query(F.data.startswith("edit:"))
async def edit_field(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not db.get_settings(cb.from_user.id):
        await cb.message.answer("Сначала пройдите первичную настройку: нажмите /start.")
        return
    field = cb.data.split(":")[1]
    if field == "regime":
        await cb.message.answer("Выберите режим:", reply_markup=regime_inline("ed"))
        return
    if field not in FIELD_PROMPTS:
        return
    await state.clear()
    await state.set_state(Edit.value)
    await state.update_data(field=field)
    await cb.message.answer(FIELD_PROMPTS[field])


@dp.callback_query(F.data.startswith("reg:ed:"))
async def edit_regime(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key not in REGIMES or not db.get_settings(cb.from_user.id):
        await cb.answer()
        return
    db.update_setting(cb.from_user.id, "regime", key)
    await cb.answer("Сохранено")
    await cb.message.answer(
        settings_text(db.get_settings(cb.from_user.id)), reply_markup=settings_inline()
    )


# --- Первичная настройка ---
@dp.callback_query(Setup.regime, F.data.startswith("reg:ob:"))
async def setup_regime(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    if key not in REGIMES:
        await cb.answer()
        return
    await state.update_data(regime=key)
    await state.set_state(Setup.margin)
    await cb.answer()
    await cb.message.answer(
        f"Режим: {REGIMES[key]['title']}\n\n"
        "2/4. Какую чистую маржу вы хотите получать (в % от цены продажи, после налогов)? Например: 20"
    )


@dp.message(Setup.regime)
async def setup_regime_hint(message: Message):
    await message.answer("Выберите режим кнопкой в сообщении выше.")


@dp.message(Setup.margin)
async def setup_margin(message: Message, state: FSMContext):
    value = parse_number(message.text)
    error = validate("margin", value)
    if error:
        await message.answer(error)
        return
    await state.update_data(margin_pct=str(value))
    await state.set_state(Setup.packaging)
    await message.answer("3/4. Сколько вы тратите на упаковку одной единицы товара, ₸? Например: 100")


@dp.message(Setup.packaging)
async def setup_packaging(message: Message, state: FSMContext):
    value = parse_number(message.text)
    error = validate("packaging", value)
    if error:
        await message.answer(error)
        return
    await state.update_data(packaging=str(value))
    await state.set_state(Setup.delivery)
    await message.answer("4/4. Сколько вы тратите на доставку одной единицы товара, ₸? Например: 100")


@dp.message(Setup.delivery)
async def setup_delivery(message: Message, state: FSMContext):
    value = parse_number(message.text)
    error = validate("delivery", value)
    if error:
        await message.answer(error)
        return
    data = await state.get_data()
    db.save_settings(
        message.from_user.id,
        data["regime"],
        Decimal(data["margin_pct"]),
        Decimal(data["packaging"]),
        value,
    )
    await state.clear()
    await message.answer(
        "✅ Настройки сохранены.\n\n" + settings_text(db.get_settings(message.from_user.id)).replace(
            "\n\nЧто изменить?", ""
        ),
        reply_markup=main_keyboard(),
    )
    await begin_calc(message, state)


@dp.message(Edit.value)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    if field not in FIELD_COLUMN:
        await state.clear()
        return
    value = parse_number(message.text)
    error = validate(field, value)
    if error:
        await message.answer(error)
        return
    db.update_setting(message.from_user.id, FIELD_COLUMN[field], value)
    await state.clear()
    await message.answer("✅ Сохранено.", reply_markup=main_keyboard())
    await message.answer(
        settings_text(db.get_settings(message.from_user.id)), reply_markup=settings_inline()
    )


# --- Сообщение об ошибке ---
@dp.message(F.text == "🐞 Сообщить об ошибке")
@dp.message(Command("report"))
async def report_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ReportState.text)
    await message.answer(
        "Опишите, что не так: что вы вводили и какой результат ожидали. "
        "Можно приложить скриншот.\n\nДля отмены нажмите «❌ Отмена».",
        reply_markup=kb([["❌ Отмена"]]),
    )


@dp.message(ReportState.text)
async def report_send(message: Message, state: FSMContext):
    if not (message.text or message.photo):
        await message.answer("Отправьте текст или фото.")
        return
    if not ADMIN_ID:
        await state.clear()
        await message.answer("Приём сообщений пока не настроен.", reply_markup=main_keyboard())
        return

    user = message.from_user
    s = db.get_settings(user.id)
    settings_line = (
        f"Режим: {REGIMES[s['regime']]['title']}, маржа {fmt(s['margin_pct'])}%, "
        f"упаковка {fmt(s['packaging'])} ₸, доставка {fmt(s['delivery'])} ₸"
        if s else "Настройки не заданы"
    )
    report_id = db.create_report(user.id)
    header = (
        f"🐞 Сообщение об ошибке №{report_id}\n"
        f"От: {user.full_name} (@{user.username or '—'}), ID: {user.id}\n"
        f"{status_text(user.id)}\n"
        f"{settings_line}\n\n"
        f"Сообщение:\n{message.text or message.caption or '(только вложение)'}\n\n"
        f"Последний расчёт клиента:\n{db.get_last_calc(user.id) or '—'}\n\n"
        "Чтобы ответить клиенту, ответьте (reply) на это сообщение."
    )
    try:
        sent = await bot.send_message(ADMIN_ID, header)
        if message.photo:
            await bot.send_photo(
                ADMIN_ID, message.photo[-1].file_id, reply_to_message_id=sent.message_id
            )
    except TelegramAPIError:
        log.exception("Не удалось отправить отчёт менеджеру")
        await message.answer("Не получилось отправить. Попробуйте позже.")
        return
    db.set_report_admin_msg(report_id, sent.message_id)
    await state.clear()
    await message.answer("Спасибо! Мы получили ваше сообщение и разберёмся.", reply_markup=main_keyboard())


# --- Покупка: клиент → менеджер ---
@dp.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    """Шаг 1: предупреждение о невозврате и запрос согласия."""
    if not ADMIN_ID:
        await cb.answer("Приём оплаты пока не настроен.", show_alert=True)
        return
    if db.get_pending_request(cb.from_user.id):
        await cb.answer("Заявка уже отправлена, менеджер скоро пришлёт QR.", show_alert=True)
        return

    await cb.answer()
    await cb.message.answer(
        "⚠️ Прочитайте перед покупкой\n\n"
        f"Подписка на 1 месяц — {PRICE} ₸.\n"
        "Возврат денег не предусмотрен: оплаченная сумма не возвращается ни полностью, "
        "ни частично, в том числе если вы перестанете пользоваться ботом до конца срока.\n\n"
        "Вы точно согласны купить подписку на этих условиях?",
        reply_markup=confirm_keyboard(),
    )


@dp.callback_query(F.data == "buy_cancel")
async def buy_cancel(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_text("Покупка отменена. Если передумаете, нажмите «💳 Подписка».")
    except TelegramAPIError:
        pass


@dp.callback_query(F.data == "buy_confirm")
async def buy_confirm(cb: CallbackQuery):
    """Шаг 2: клиент согласился, создаём заявку и отправляем менеджеру."""
    if not ADMIN_ID:
        await cb.answer("Приём оплаты пока не настроен.", show_alert=True)
        return

    user = cb.from_user
    # Убираем кнопки, чтобы нельзя было нажать повторно
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    if db.get_pending_request(user.id):
        await cb.answer("Заявка уже отправлена, менеджер скоро пришлёт QR.", show_alert=True)
        return

    req_id = db.create_request(user.id, PRICE)
    try:
        sent = await bot.send_message(
            ADMIN_ID,
            f"🧾 Заявка №{req_id} на подписку\n"
            f"Клиент: {user.full_name} (@{user.username or '—'})\n"
            f"ID: {user.id}\nСумма: {PRICE} ₸ за 1 месяц\n"
            "Клиент подтвердил, что возврата денег нет.\n\n"
            "1) Ответьте (reply) на это сообщение фото QR, бот перешлёт его клиенту.\n"
            "2) Когда оплата придёт, нажмите «Подтвердить оплату».",
            reply_markup=manager_keyboard(req_id),
        )
    except TelegramAPIError:
        log.exception("Не удалось отправить заявку менеджеру")
        db.resolve_request(req_id, "failed")
        await cb.answer("Не получилось отправить заявку. Попробуйте позже.", show_alert=True)
        return

    db.set_admin_msg(req_id, sent.message_id)
    await cb.answer()
    await cb.message.answer(
        "✅ Заявка отправлена менеджеру. Он пришлёт QR для оплаты прямо сюда, в чат с ботом."
    )


# --- Менеджер отвечает на заявку фото QR ---
def _is_request_reply(message: Message) -> bool:
    if not message.reply_to_message:
        return False
    req = db.get_request_by_admin_msg(message.reply_to_message.message_id)
    return bool(req and req["status"] == "pending")


@dp.message(F.from_user.id == ADMIN_ID, F.reply_to_message, _is_request_reply)
async def manager_sends_qr(message: Message):
    req = db.get_request_by_admin_msg(message.reply_to_message.message_id)
    caption = f"💳 Оплатите {req['amount']} ₸ по QR.\nПосле оплаты нажмите «✅ Я оплатил»."
    try:
        if message.photo or message.document:
            await message.copy_to(req["user_id"], caption=caption, reply_markup=paid_keyboard(req["id"]))
        elif message.text:
            await bot.send_message(
                req["user_id"], f"{message.text}\n\n{caption}", reply_markup=paid_keyboard(req["id"])
            )
        else:
            await message.answer("Отправьте QR фото, файлом или текстом (ссылкой).")
            return
    except TelegramAPIError:
        log.exception("Не удалось отправить QR клиенту")
        await message.answer("⚠️ Не удалось отправить клиенту (возможно, он заблокировал бота).")
        return
    await message.answer(f"📨 QR по заявке №{req['id']} отправлен клиенту.")


# --- Менеджер отвечает клиенту на сообщение об ошибке ---
def _is_report_reply(message: Message) -> bool:
    if not message.reply_to_message:
        return False
    return bool(db.get_report_by_admin_msg(message.reply_to_message.message_id))


@dp.message(F.from_user.id == ADMIN_ID, F.reply_to_message, _is_report_reply)
async def manager_answers_report(message: Message):
    report = db.get_report_by_admin_msg(message.reply_to_message.message_id)
    try:
        if message.text:
            await bot.send_message(report["user_id"], f"💬 Ответ на ваше сообщение об ошибке:\n\n{message.text}")
        else:
            await message.copy_to(report["user_id"])
    except TelegramAPIError:
        log.exception("Не удалось отправить ответ клиенту")
        await message.answer("⚠️ Не удалось отправить клиенту (возможно, он заблокировал бота).")
        return
    await message.answer("📨 Ответ отправлен клиенту.")


# --- Клиент сообщает об оплате ---
@dp.callback_query(F.data.startswith("paid:"))
async def paid(cb: CallbackQuery):
    req_id = int(cb.data.split(":")[1])
    req = db.get_request(req_id)
    if not req or req["user_id"] != cb.from_user.id or req["status"] != "pending":
        await cb.answer("Эта заявка уже обработана.", show_alert=True)
        return
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 Клиент по заявке №{req_id} нажал «Я оплатил». Проверьте поступление и подтвердите.",
            reply_to_message_id=req["admin_msg_id"],
        )
    except TelegramAPIError:
        log.exception("Не удалось уведомить менеджера")
    await cb.answer("Менеджер получил уведомление.")
    await cb.message.answer("Спасибо! Менеджер проверит оплату и откроет доступ.")


# --- Менеджер подтверждает / отклоняет ---
@dp.callback_query(F.data.startswith("approve:"))
async def approve(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    req_id = int(cb.data.split(":")[1])
    req = db.get_request(req_id)
    if not req or not db.resolve_request(req_id, "approved"):
        await cb.answer("Заявка уже обработана.", show_alert=True)
        return

    until = db.grant_subscription(req["user_id"])
    await cb.message.edit_text(
        cb.message.text + f"\n\n✅ Подтверждено. Подписка до {fmt_date(until)}"
    )
    try:
        await bot.send_message(
            req["user_id"],
            f"✅ Оплата подтверждена! Подписка активна до {fmt_date(until)}.",
            reply_markup=main_keyboard(),
        )
    except TelegramAPIError:
        log.exception("Не удалось уведомить клиента о подтверждении")
    await cb.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def reject(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    req_id = int(cb.data.split(":")[1])
    req = db.get_request(req_id)
    if not req or not db.resolve_request(req_id, "rejected"):
        await cb.answer("Заявка уже обработана.", show_alert=True)
        return

    await cb.message.edit_text(cb.message.text + "\n\n❌ Отклонено")
    try:
        await bot.send_message(
            req["user_id"],
            "Оплату подтвердить не удалось. Если вы платили, напишите менеджеру.",
        )
    except TelegramAPIError:
        log.exception("Не удалось уведомить клиента об отказе")
    await cb.answer()


@dp.message(Command("grant"))
async def grant(message: Message, command: CommandObject):
    """Ручная выдача подписки менеджером: /grant <user_id> [дней]"""
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = (command.args or "").split()
        user_id = int(parts[0])
        days = int(parts[1]) if len(parts) > 1 else db.SUB_DAYS
    except (ValueError, IndexError):
        await message.answer("Формат: /grant <user_id> [дней]")
        return
    until = db.grant_subscription(user_id, days)
    await message.answer(f"Готово: подписка {user_id} до {fmt_date(until)}.")


# --- Расчёт ---
@dp.message(CalcState.category)
async def category(message: Message, state: FSMContext):
    name = message.text
    if name == "❌ Отмена":
        await cancel(message, state)
        return
    if name not in TARIFFS:
        await message.answer("Выбери категорию кнопкой ниже.", reply_markup=categories_keyboard())
        return

    await state.update_data(category=name)
    if name == "🛢 Масла и технические жидкости":
        await state.set_state(CalcState.volume)
        await message.answer(
            "Выбери объём канистры/упаковки:",
            reply_markup=volume_keyboard(),
        )
    else:
        await state.set_state(CalcState.purchase_price)
        await message.answer(
            f"Категория: {name}\nКомиссия Kaspi: {TARIFFS[name]['commission_gross'] * 100:.2f}% с НДС.\n\n"
            "Введи закупочную цену С НДС (ту сумму, которую вы платите поставщику)."
        )


@dp.message(CalcState.volume)
async def volume(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await cancel(message, state)
        return
    if message.text == "↩️ Назад":
        await state.set_state(CalcState.category)
        await message.answer("Выбери категорию:", reply_markup=categories_keyboard())
        return
    if message.text not in OIL_VOLUMES:
        await message.answer("Выбери объём кнопкой ниже.", reply_markup=volume_keyboard())
        return

    await state.update_data(volume=message.text)
    await state.set_state(CalcState.purchase_price)
    await message.answer(f"Объём: {message.text}\n\nВведи закупочную цену этой упаковки С НДС.")


def format_result(category_name: str, volume_text, s: dict, r) -> str:
    regime = REGIMES[s["regime"]]
    if regime["vat"]:
        purchase_lines = (
            f"Закупка с НДС: {money(r.purchase_gross)} ₸\n"
            f"Закупка без НДС: {money(r.purchase_net)} ₸\n"
            f"Входной НДС: {money(r.input_vat)} ₸\n"
        )
    else:
        purchase_lines = f"Закупка: {money(r.purchase_gross)} ₸ (НДС не возмещается)\n"

    volume_line = f"Объём: {volume_text}\n" if volume_text else ""
    return (
        "📊 РЕЗУЛЬТАТ\n\n"
        f"Режим: {regime['title']}\n"
        f"Категория: {category_name}\n"
        f"{volume_line}"
        f"{purchase_lines}\n"
        f"Kaspi: {r.kaspi_rate * 100:.2f}%\n"
        f"Доставка: {money(r.delivery)} ₸\n"
        f"Упаковка: {money(r.packaging)} ₸\n"
        f"{regime['tax_label']}: {money(r.tax)} ₸\n\n"
        f"🔻 Минимальная цена: {money(r.minimum_price)} ₸\n"
        f"🔺 Рекомендуемая цена: {money(r.recommended_price)} ₸\n\n"
        f"Чистая прибыль при минимальной цене: {money(r.net_profit)} ₸\n"
        f"Чистая маржа: {r.margin * 100:.2f}%"
    )


@dp.message(CalcState.purchase_price)
async def purchase_price(message: Message, state: FSMContext):
    cost = parse_number(message.text)
    if cost is None or cost <= 0:
        await message.answer("Введите положительную сумму, например 10000 или 12500.50")
        return

    # Повторная проверка доступа прямо перед выдачей результата
    # (если подписка закончилась, пока пользователь вводил данные)
    if not await ensure_access(message, state):
        return

    s = db.get_settings(message.from_user.id)
    if not s:
        await start_setup(message, state)
        return

    data = await state.get_data()
    category_name = data["category"]
    tariff = TARIFFS[category_name]
    try:
        result = calculate_price(
            cost,
            tariff["commission_gross"],
            s["regime"],
            s["margin_pct"] / Decimal("100"),
            s["delivery"],
            s["packaging"],
        )
    except ValueError:
        await message.answer(
            "С такой маржей и налогами цену рассчитать нельзя: расходы съедают всю выручку. "
            "Уменьшите маржу в «⚙️ Мои настройки»."
        )
        return

    text = format_result(category_name, data.get("volume"), s, result)
    await message.answer(text)
    db.save_last_calc(message.from_user.id, text)

    # Списываем бесплатный расчёт только после успешного результата (админу не списываем)
    if not is_admin(message.from_user.id):
        db.register_use(message.from_user.id)
    await state.clear()
    await message.answer(
        f"{status_text(message.from_user.id)}\nДля нового товара нажми «🧮 Новый расчёт».",
        reply_markup=main_keyboard(),
    )


async def reminder_loop():
    """Раз в час рассылает напоминания тем, у кого подписка скоро закончится."""
    while True:
        try:
            for user_id, until, raw in db.get_users_to_remind(REMIND_DAYS):
                if is_admin(user_id):
                    db.mark_reminded(user_id, raw)
                    continue
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ Ваша подписка заканчивается {fmt_date(until)}.\n\n"
                        "Чтобы не потерять доступ к расчётам, продлите её заранее: "
                        "новый месяц добавится к остатку.\n\n"
                        "⚠️ Возврат денег не предусмотрен.",
                        reply_markup=buy_keyboard(),
                    )
                except TelegramForbiddenError:
                    pass  # клиент заблокировал бота, повторять не нужно
                except TelegramAPIError:
                    log.exception("Не удалось отправить напоминание %s, повторим позже", user_id)
                    continue
                db.mark_reminded(user_id, raw)
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(3600)


async def main():
    reminder_task = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
