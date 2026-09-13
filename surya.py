# -*- coding: utf-8 -*-
import asyncio
import io
import logging
import urllib.parse
import os
from aiohttp import web
from collections import defaultdict

import aiosqlite
import segno
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = "8934440785:AAFrXovA9gquO3ivXkgZ2dvhYicAOBQs-dU"
ADMIN_IDS = [1113601002]
DB_NAME = "suryabot_database.db"

logging.basicConfig(level=logging.INFO)

session = AiohttpSession(timeout=60.0)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# MESSAGE AUTO-DELETE & TRACKING SYSTEM
# ==========================================
user_messages = defaultdict(list)


def track(chat_id: int, message_id: int):
    if message_id not in user_messages[chat_id]:
        user_messages[chat_id].append(message_id)


async def delete_old_messages(chat_id: int, exclude_ids: list[int] | None = None):
    exclude = set(exclude_ids or [])
    all_ids = [mid for mid in user_messages.get(chat_id, []) if mid not in exclude]
    user_messages[chat_id] = [mid for mid in user_messages.get(chat_id, []) if mid in exclude]

    if not all_ids:
        return

    for i in range(0, len(all_ids), 100):
        chunk = all_ids[i : i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except TelegramBadRequest:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"Failed to delete message chunk: {e}")


class MessageTrackerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.TelegramObject, data: dict):
        if isinstance(event, types.Message):
            track(event.chat.id, event.message_id)
        return await handler(event, data)


# ==========================================
# DATABASE LAYER
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                premium_status TEXT DEFAULT 'Free',
                is_banned INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_name TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                name TEXT,
                amount REAL,
                validity TEXT
            )
        """)

        defaults = {
            "maintenance": "off",
            "upi_id": "paytm.s21dj6b@pty",
            "payee_name": "NAZIYA NASRIN",
            "welcome_photo": "https://kommodo.ai/i/vMd2KH7PZC8bgMH9mGWm",
            "welcome_text": (
                "\U0001F44B Welcome to Our Bot!\n\n"
                "\u2728 Explore features, view demos, check subscriptions, "
                "or manage your account using the buttons below."
            ),
            "plans_text": (
                "\U0001F4E6 Choose Your Membership Plan\n\n"
                "\U0001F449 Select any plan below to get an instant UPI QR payment card:"
            ),
            "demo_video": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
        }
        for k, v in defaults.items():
            # Using INSERT OR REPLACE ensures updated clean strings overwrite corrupted ones
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )

        default_plans = [
            ("plan_1", "INDIAN WEBSERIES", 99.0, "30 Days"),
            ("plan_2", "3 MONTHS SPECIAL", 249.0, "90 Days"),
            ("plan_3", "6 MONTHS VIP", 449.0, "180 Days"),
            ("plan_4", "1 YEAR ACCESS", 799.0, "365 Days"),
            ("plan_5", "LIFETIME PASS", 1299.0, "Lifetime"),
            ("plan_6", "4K ULTRA STREAM", 199.0, "30 Days"),
            ("plan_7", "PRO PASS", 349.0, "60 Days"),
            ("plan_8", "EXCLUSIVE HUB", 599.0, "90 Days"),
        ]
        for p in default_plans:
            await db.execute(
                "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity) VALUES (?, ?, ?, ?)",
                p,
            )

        await db.commit()


async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def update_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE settings SET value = ? WHERE key = ?", (value, key)
        )
        await db.commit()


async def get_all_plans():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT plan_id, name, amount, validity FROM plans ORDER BY plan_id ASC"
        ) as cur:
            return await cur.fetchall()


async def get_plan(plan_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT plan_id, name, amount, validity FROM plans WHERE plan_id = ?",
            (plan_id,),
        ) as cur:
            return await cur.fetchone()


async def update_plan(plan_id: str, name: str, amount: float, validity: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE plans SET name = ?, amount = ?, validity = ? WHERE plan_id = ?",
            (name, amount, validity, plan_id),
        )
        await db.commit()


async def add_or_update_user(user: types.User):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, full_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username
        """,
            (user.id, user.full_name, user.username or "N/A"),
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchone()


async def get_user_payment_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT 
                COUNT(CASE WHEN status = 'approved' THEN 1 END),
                COUNT(CASE WHEN status = 'pending' THEN 1 END),
                COUNT(*)
            FROM payments WHERE user_id = ?
        """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return {
                "approved": row[0] or 0,
                "pending": row[1] or 0,
                "total": row[2] or 0,
            }


async def set_user_ban_status(user_id: int, is_banned: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (is_banned, user_id),
        )
        await db.commit()


async def generate_upi_qr(plan_name: str, amount: float) -> io.BytesIO:
    upi_id = await get_setting("upi_id")
    payee_name = await get_setting("payee_name")
    upi_params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"Payment for {plan_name}",
    }
    upi_url = "upi://pay?" + urllib.parse.urlencode(upi_params)
    qr = segno.make(upi_url, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=8, border=2)
    buffer.seek(0)
    return buffer


# ==========================================
# MIDDLEWARE SETUP
# ==========================================
class SecurityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.TelegramObject, data: dict):
        user = data.get("event_from_user")
        if not user or user.id in ADMIN_IDS:
            return await handler(event, data)
        user_record = await get_user(user.id)
        if user_record and user_record[5] == 1:
            if isinstance(event, types.Message):
                await event.answer("You are banned from using this bot.")
            return
        if await get_setting("maintenance") == "on":
            if isinstance(event, types.Message):
                await event.answer(
                    "Bot is under maintenance. Please try again later."
                )
            return
        return await handler(event, data)


dp.message.outer_middleware(MessageTrackerMiddleware())
dp.message.outer_middleware(SecurityMiddleware())
dp.callback_query.outer_middleware(SecurityMiddleware())


# ==========================================
# FSM STATES
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_lookup = State()
    waiting_for_welcome_photo = State()
    waiting_for_welcome_text = State()
    waiting_for_plans_text = State()
    waiting_for_demo_video = State()
    waiting_for_upi_id = State()
    waiting_for_payee_name = State()
    waiting_for_plan_name = State()
    waiting_for_plan_price = State()
    waiting_for_plan_validity = State()


class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()


# ==========================================
# KEYBOARDS
# ==========================================
def get_home_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001F3AC View Demo", callback_data="btn_view_demo")
    builder.button(text="\u2B50 My Premium", callback_data="btn_my_premium")
    builder.button(text="\U0001F464 My Profile", callback_data="btn_my_profile")
    builder.adjust(1)
    return builder.as_markup()


def get_demo_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001F48E Get Premium", callback_data="btn_get_premium")
    builder.button(text="\U0001F3E0 Home", callback_data="btn_home")
    builder.adjust(2)
    return builder.as_markup()


async def get_8_plans_keyboard():
    builder = InlineKeyboardBuilder()
    plans = await get_all_plans()
    for pid, name, price, _ in plans:
        builder.button(
            text=f"\U0001F525 {name} (Rs.{int(price)})", callback_data=f"buy_plan:{pid}"
        )
    builder.button(text="\U0001F3E0 Home", callback_data="btn_home")
    builder.adjust(*(1 for _ in range(len(plans) + 1)))
    return builder.as_markup()


def get_upi_card_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001F4E5 CHECK PAYMENT", callback_data="check_payment")
    builder.button(text="\U0001F519 BACK TO PLANS", callback_data="btn_get_premium")
    builder.adjust(1)
    return builder.as_markup()


async def get_admin_menu():
    maint = await get_setting("maintenance")
    builder = InlineKeyboardBuilder()
    builder.button(text="Stats", callback_data="admin_stats")
    builder.button(text="User Lookup", callback_data="admin_lookup")
    builder.button(text="Broadcast", callback_data="admin_broadcast")
    builder.button(
        text=f"Maint: {maint.upper()}", callback_data="admin_toggle_maint"
    )
    builder.button(text="Edit Photo", callback_data="adm_edit_photo")
    builder.button(text="Edit Welcome Msg", callback_data="adm_edit_wtext")
    builder.button(text="Edit Plans Msg", callback_data="adm_edit_ptext")
    builder.button(text="Edit Demo Video", callback_data="adm_edit_video")
    builder.button(text="Edit Payment UPI", callback_data="adm_edit_upi")
    builder.button(
        text="Edit Plan Buttons", callback_data="adm_edit_plans_list"
    )
    builder.button(text="Close", callback_data="admin_close")
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def get_admin_user_card_keyboard(target_id: int, is_banned: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Unban User" if is_banned else "Ban User",
        callback_data=f"adm_ban:{target_id}:{0 if is_banned else 1}",
    )
    builder.button(text="Back", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


# ==========================================
# FLOW HANDLERS WITH AUTO-DELETE
# ==========================================
async def send_welcome_flow(chat_id: int):
    photo_url = await get_setting("welcome_photo")
    caption = await get_setting("welcome_text")
    try:
        m1 = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=caption,
            reply_markup=get_home_keyboard(),
        )
    except Exception:
        m1 = await bot.send_message(
            chat_id=chat_id, text=caption, reply_markup=get_home_keyboard()
        )
    track(chat_id, m1.message_id)

    plans_txt = await get_setting("plans_text")
    m2 = await bot.send_message(
        chat_id=chat_id,
        text=plans_txt,
        reply_markup=await get_8_plans_keyboard(),
    )
    track(chat_id, m2.message_id)


@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await add_or_update_user(message.from_user)
    await send_welcome_flow(message.chat.id)


@dp.callback_query(F.data == "btn_home")
async def nav_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    
    await delete_old_messages(callback.message.chat.id)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    except Exception as e:
        logging.debug(f"Error deleting message: {e}")
        
    await send_welcome_flow(callback.message.chat.id)


@dp.callback_query(F.data == "btn_get_premium")
async def nav_plans(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    text = await get_setting("plans_text")
    msg = await bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=await get_8_plans_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@dp.callback_query(F.data == "btn_view_demo")
async def nav_demo(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    video_url = await get_setting("demo_video")
    try:
        msg = await bot.send_video(
            chat_id=callback.message.chat.id,
            video=video_url,
            caption="\U0001F4FA Demo Video",
            reply_markup=get_demo_keyboard(),
        )
    except Exception:
        msg = await bot.send_message(
            chat_id=callback.message.chat.id,
            text="\U0001F4FA Demo video temporarily unavailable.",
            reply_markup=get_demo_keyboard(),
        )
    track(callback.message.chat.id, msg.message_id)


@dp.callback_query(F.data == "btn_my_premium")
async def nav_my_premium(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    user = await get_user(callback.from_user.id)
    plan = user[4] if user else "Free"
    text = f"\u2B50 My Premium Membership\n\nPlan: {plan}\nStatus: {'Active' if plan != 'Free' else 'Free Tier'}"
    msg = await bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=get_demo_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@dp.callback_query(F.data == "btn_my_profile")
async def nav_my_profile(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    user = await get_user(callback.from_user.id)
    if not user:
        return
    uid, full_name, username, joined_at, plan, _ = user
    payments = await get_user_payment_stats(uid)
    text = (
        f"\U0001F464 MY PROFILE\n"
        f"Name: {full_name}\n"
        f"Username: @{username}\n"
        f"ID: {uid}\n"
        f"Joined: {joined_at}\n"
        f"Plan: {plan}\n"
        f"Payments: Approved: {payments['approved']} | Pending: {payments['pending']}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001F48E Get Premium", callback_data="btn_get_premium")
    builder.button(text="\U0001F3E0 Home", callback_data="btn_home")
    builder.adjust(2)
    msg = await bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=builder.as_markup(),
    )
    track(callback.message.chat.id, msg.message_id)


# ==========================================
# UPI CARD & PROOF HANDLERS
# ==========================================
@dp.callback_query(F.data.startswith("buy_plan:"))
async def process_plan_selection(
    callback: types.CallbackQuery, state: FSMContext
):
    await callback.answer()
    
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    pid = callback.data.split(":")[1]
    plan = await get_plan(pid)
    if not plan:
        return
    _, plan_name, amount, validity = plan
    qr_buf = await generate_upi_qr(plan_name, amount)
    photo_file = BufferedInputFile(qr_buf.getvalue(), filename="qr.png")
    upi_id = await get_setting("upi_id")
    payee = await get_setting("payee_name")

    caption = (
        f"\U0001F4B3 UPI PAYMENT\n"
        f"Plan: {plan_name}\n"
        f"Amount: Rs.{amount:.2f}\n"
        f"Validity: {validity}\n\n"
        f"\U0001F464 Name: {payee}\n"
        f"\U0001F511 UPI ID: {upi_id}\n\n"
        f"1\uFE0F\u20E3 Scan QR and pay\n"
        f"2\uFE0F\u20E3 Tap CHECK PAYMENT and upload screenshot"
    )
    await state.update_data(current_plan=plan_name, current_amount=amount)
    msg = await bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=photo_file,
        caption=caption,
        reply_markup=get_upi_card_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@dp.callback_query(F.data == "check_payment")
async def handle_check_payment(
    callback: types.CallbackQuery, state: FSMContext
):
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    await state.set_state(PaymentStates.waiting_for_screenshot)
    msg = await bot.send_message(
        chat_id=callback.message.chat.id,
        text="\U0001F4F8 Upload payment screenshot. /cancel to abort.",
    )
    track(callback.message.chat.id, msg.message_id)


@dp.message(PaymentStates.waiting_for_screenshot, F.photo)
async def process_payment_proof(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_name = data.get("current_plan", "Unknown Plan")
    amount = data.get("current_amount", 0)

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO payments (user_id, plan_name, amount) VALUES (?, ?, ?)",
            (message.from_user.id, plan_name, amount),
        )
        pid = cur.lastrowid
        await db.commit()

    await state.clear()

    msg = await message.answer(
        "\u2705 Screenshot Received! Verification is in progress.",
        reply_markup=get_home_keyboard(),
    )
    track(message.chat.id, msg.message_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="\u2705 Approve", callback_data=f"adm_pay:{pid}:approved")
    builder.button(text="\u274C Reject", callback_data=f"adm_pay:{pid}:rejected")
    builder.adjust(2)

    caption = (
        f"\U0001F514 <b>New Payment Screenshot Received</b>\n\n"
        f"<b>Order ID:</b> #{pid}\n"
        f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{message.from_user.username or 'N/A'}\n"
        f"<b>Plan:</b> {plan_name}\n"
        f"<b>Amount:</b> Rs.{amount}"
    )

    photo_file_id = message.photo[-1].file_id

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
            )
            logging.info(f"Payment proof #{pid} successfully sent to admin {admin_id}")
        except Exception as e:
            logging.error(f"Failed to send photo to admin {admin_id}: {e}")
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"{caption}\n\n\u26A0\uFE0F <i>(Screenshot could not be loaded directly)</i>",
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                )
            except Exception as inner_e:
                logging.error(
                    f"Admin {admin_id} unreachable. Ensure admin has sent /start to the bot: {inner_e}"
                )


@dp.message(PaymentStates.waiting_for_screenshot)
async def invalid_proof(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await send_welcome_flow(message.chat.id)
        return
    msg = await message.answer("\u26A0\uFE0F Please send an image screenshot.")
    track(message.chat.id, message.message_id)


# ==========================================
# ADMIN CONTROL PANEL
# ==========================================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "Admin Control Panel", reply_markup=await get_admin_menu()
        )


@dp.callback_query(F.data == "admin_home")
async def nav_admin_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.from_user.id in ADMIN_IDS:
        await callback.message.edit_text(
            "Admin Control Panel", reply_markup=await get_admin_menu()
        )
        await callback.answer()


@dp.callback_query(F.data == "admin_close")
async def close_admin_panel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data == "admin_toggle_maint")
async def handle_maintenance_toggle(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        curr = await get_setting("maintenance")
        await update_setting("maintenance", "off" if curr == "on" else "on")
        await callback.message.edit_reply_markup(
            reply_markup=await get_admin_menu()
        )
        await callback.answer("Maintenance toggled.")


@dp.callback_query(F.data == "adm_edit_photo")
async def start_edit_welcome_photo(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_welcome_photo)
        await callback.message.edit_text(
            "Send the new photo or URL:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_welcome_photo)
async def process_new_welcome_photo(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    target = message.photo[-1].file_id if message.photo else message.text
    await update_setting("welcome_photo", target)
    await state.clear()
    await message.answer(
        "Welcome photo updated!", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data == "adm_edit_wtext")
async def start_edit_welcome_text(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_welcome_text)
        await callback.message.edit_text(
            "Send the new welcome text:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_welcome_text)
async def process_new_welcome_text(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await update_setting("welcome_text", message.text)
    await state.clear()
    await message.answer(
        "Welcome text updated!", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data == "adm_edit_ptext")
async def start_edit_plans_text(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_plans_text)
        await callback.message.edit_text(
            "Send the new plans text:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_plans_text)
async def process_new_plans_text(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await update_setting("plans_text", message.text)
    await state.clear()
    await message.answer(
        "Plans message text updated!", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data == "adm_edit_video")
async def start_edit_demo_video(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_demo_video)
        await callback.message.edit_text(
            "Send the new video or URL:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_demo_video)
async def process_new_demo_video(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    target = message.video.file_id if message.video else message.text
    await update_setting("demo_video", target)
    await state.clear()
    await message.answer(
        "Demo video updated!", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data == "adm_edit_upi")
async def start_edit_upi(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_upi_id)
        await callback.message.edit_text(
            "Step 1: Enter new UPI ID:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_upi_id)
async def process_new_upi_id(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.update_data(new_upi_id=message.text.strip())
    await state.set_state(AdminStates.waiting_for_payee_name)
    await message.answer("Step 2: Enter new Payee Name:")


@dp.message(AdminStates.waiting_for_payee_name)
async def process_new_payee_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    d = await state.get_data()
    await update_setting("upi_id", d["new_upi_id"])
    await update_setting("payee_name", message.text.strip())
    await state.clear()
    await message.answer(
        "UPI details updated!", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data == "adm_edit_plans_list")
async def show_plans_for_editing(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        plans = await get_all_plans()
        builder = InlineKeyboardBuilder()
        for pid, name, price, _ in plans:
            builder.button(
                text=f"{name} (Rs.{int(price)})", callback_data=f"adm_psel:{pid}"
            )
        builder.button(text="Back", callback_data="admin_home")
        builder.adjust(1)
        await callback.message.edit_text(
            "Select a button/plan to edit:", reply_markup=builder.as_markup()
        )
        await callback.answer()


@dp.callback_query(F.data.startswith("adm_psel:"))
async def select_plan_to_edit(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        pid = callback.data.split(":")[1]
        await state.update_data(target_pid=pid)
        await state.set_state(AdminStates.waiting_for_plan_name)
        await callback.message.edit_text(
            f"Step 1: Send new Plan Name for `{pid}`:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_plan_name)
async def process_edit_plan_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.update_data(new_pname=message.text.strip())
    await state.set_state(AdminStates.waiting_for_plan_price)
    await message.answer("Step 2: Enter new Price (e.g. 199):")


@dp.message(AdminStates.waiting_for_plan_price)
async def process_edit_plan_price(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    try:
        price = float(message.text.strip())
        await state.update_data(new_pprice=price)
        await state.set_state(AdminStates.waiting_for_plan_validity)
        await message.answer("Step 3: Enter new Validity (e.g. 30 Days):")
    except ValueError:
        await message.answer("Please enter a numeric price.")


@dp.message(AdminStates.waiting_for_plan_validity)
async def process_edit_plan_validity(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    d = await state.get_data()
    await update_plan(
        d["target_pid"],
        d["new_pname"],
        d["new_pprice"],
        message.text.strip(),
    )
    await state.clear()
    await message.answer("Plan updated!", reply_markup=await get_admin_menu())


@dp.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                tot = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE is_banned=1"
            ) as cur:
                ban = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*), SUM(amount) FROM payments WHERE status='approved'"
            ) as cur:
                row = await cur.fetchone()
                sales, rev = row[0] or 0, row[1] or 0.0
        builder = InlineKeyboardBuilder()
        builder.button(text="Back", callback_data="admin_home")
        await callback.message.edit_text(
            f"Analytics:\nTotal Users: {tot}\nActive: {tot - ban}\nBanned: {ban}\nApproved Orders: {sales}\nRevenue: Rs.{rev:.2f}",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()


@dp.callback_query(F.data == "admin_lookup")
async def start_user_lookup(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_lookup)
        await callback.message.edit_text(
            "Send numeric Telegram ID:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_lookup)
async def process_user_lookup(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    if not message.text.isdigit():
        await message.answer("Send digits only.")
        return
    u = await get_user(int(message.text))
    await state.clear()
    if not u:
        await message.answer(
            "User not found.", reply_markup=await get_admin_menu()
        )
        return
    uid, name, uname, joined, plan, ban = u
    await message.answer(
        f"ID: {uid}\nName: {name}\nUsername: @{uname}\nPlan: {plan}\nStatus: {'Banned' if ban else 'Active'}",
        reply_markup=get_admin_user_card_keyboard(uid, ban),
    )


@dp.callback_query(F.data.startswith("adm_ban:"))
async def handle_admin_ban(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        _, uid, st = callback.data.split(":")
        await set_user_ban_status(int(uid), int(st))
        u = await get_user(int(uid))
        await callback.message.edit_text(
            f"User {uid} updated. Status: {'Banned' if u[5] else 'Active'}",
            reply_markup=get_admin_user_card_keyboard(int(uid), u[5]),
        )
        await callback.answer("Updated.")


@dp.callback_query(F.data == "admin_broadcast")
async def start_admin_broadcast(
    callback: types.CallbackQuery, state: FSMContext
):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.edit_text(
            "Send message to broadcast:\n/cancel to abort."
        )
        await callback.answer()


@dp.message(AdminStates.waiting_for_broadcast)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.clear()
    status_msg = await message.answer("Broadcasting...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id FROM users WHERE is_banned=0"
        ) as cur:
            users = await cur.fetchall()
    sent = 0
    for (uid,) in users:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await message.copy_to(chat_id=uid)
                sent += 1
            except Exception:
                pass
        except Exception:
            pass
    await status_msg.edit_text(
        f"Broadcast finished. Sent: {sent}", reply_markup=await get_admin_menu()
    )


@dp.callback_query(F.data.startswith("adm_pay:"))
async def handle_admin_pay_approval(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        _, pid, act = callback.data.split(":")
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id, plan_name FROM payments WHERE id=?",
                (int(pid),),
            ) as cur:
                p = await cur.fetchone()
            if not p:
                await callback.answer("Not found.")
                return
            t_uid, pl = p
            if act == "approved":
                await db.execute(
                    "UPDATE payments SET status='approved' WHERE id=?",
                    (int(pid),),
                )
                await db.execute(
                    "UPDATE users SET premium_status=? WHERE user_id=?",
                    (pl, t_uid),
                )
                await db.commit()
                try:
                    await bot.send_message(
                        t_uid,
                        f"Payment Approved! Your plan {pl} is active!",
                    )
                except Exception:
                    pass
                await callback.message.edit_caption(
                    caption=callback.message.caption + "\n\nSTATUS: APPROVED"
                )
            else:
                await db.execute(
                    "UPDATE payments SET status='rejected' WHERE id=?",
                    (int(pid),),
                )
                await db.commit()
                try:
                    await bot.send_message(
                        t_uid, "Payment verification failed."
                    )
                except Exception:
                    pass
                await callback.message.edit_caption(
                    caption=callback.message.caption + "\n\nSTATUS: REJECTED"
                )
        await callback.answer("Status updated.")
# -------------------------------------------------------------
# DUMMY WEB SERVER FOR RENDER KEEP-ALIVE
# -------------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# -------------------------------------------------------------
# LAUNCH BOT
# -------------------------------------------------------------
async def main():
    await init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning(f"Could not drop webhook: {e}")
    
    await start_web_server()
    print("Bot is up and running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
