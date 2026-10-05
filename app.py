import os
import hmac
import hashlib
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, types
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from sqlalchemy import Column, Float, String, BigInteger, Boolean
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.future import select

# --- 1. НАСТРОЙКИ И КОНСТАНТЫ ---
TELEGRAM_TOKEN = "8983015392:AAEP4SykIhK_TpwPLLzRNi-2-K4sEHMbRco"
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"
WEBAPP_URL = "https://almaz-shop.vercel.app"
ADMIN_USER_ID = 7334078827

logging.basicConfig(level=logging.INFO)

# --- 2. БАЗА ДАННЫХ (Async SQLite) ---
DATABASE_URL = "sqlite+aiosqlite:///./almaz_shop.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()


class UserDB(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True, index=True)
    balance = Column(Float, default=0.0)


class TransactionDB(Base):
    __tablename__ = "transactions"
    invoice_id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    processed = Column(Boolean, default=True)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- 3. ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА (Строго до хендлеров!) ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# Состояния для FSM (выдача баланса пользователю)
class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()


# --- 4. FASTAPI И LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создание таблиц при запуске
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Запуск фонового поллинга Telegram бота
    polling_task = asyncio.create_task(dp.start_polling(bot))
    logging.info("✅ База данных подключена, Telegram бот успешно запущен!")

    yield

    polling_task.cancel()
    await bot.session.close()


app = FastAPI(title="Almaz Shop Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 5. PYDANTIC СХЕМЫ ---
class InvoiceRequest(BaseModel):
    amount: float
    user_id: int


class AdminActionRequest(BaseModel):
    admin_id: int
    target_user_id: int
    amount: float


# --- 6. API ЭНДПОИНТЫ (FASTAPI) ---
@app.get("/")
async def root():
    return {"status": "ok", "message": "Almaz Shop Backend Active"}


@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserDB).where(UserDB.user_id == user_id))
    user = result.scalars().first()

    if not user:
        user = UserDB(user_id=user_id, balance=0.0)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return {"balance": user.balance}


@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
    if data.amount < 1.0:
        raise HTTPException(status_code=400, detail="Минимальная сумма пополнения: $1")

    import httpx
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(data.amount),
        "payload": str(data.user_id)
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            res_data = response.json()
    except Exception as e:
        logging.error(f"CryptoBot Connection Error: {e}")
        raise HTTPException(status_code=500, detail="Ошибка соединения с платежным шлюзом")

    if not res_data.get("ok"):
        raise HTTPException(status_code=500, detail="Ошибка API CryptoBot")

    result = res_data["result"]
    raw_url = result.get("pay_url") or result.get("bot_invoice_url", "")
    pay_url = raw_url.replace("t.me/CryptoPayBot", "t.me/CryptoBot")

    return {
        "pay_url": pay_url,
        "invoice_id": result.get("invoice_id")
    }


@app.post("/crypto-webhook")
async def crypto_webhook(
    request: Request,
    crypto_pay_api_signature: str = Header(None),
    db: AsyncSession = Depends(get_db)
):
    body_bytes = await request.body()

    # Проверка подлинности HMAC подписи CryptoBot
    if CRYPTO_BOT_TOKEN and crypto_pay_api_signature:
        secret = hashlib.sha256(CRYPTO_BOT_TOKEN.encode()).digest()
        check_signature = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        if check_signature != crypto_pay_api_signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

    update = await request.json()

    if update.get("update_type") == "invoice_paid":
        payload_data = update.get("payload", {})
        invoice_id = int(payload_data.get("invoice_id", 0))
        user_id = int(payload_data.get("payload", 0))
        amount_usd = float(payload_data.get("amount", 0.0))

        if user_id > 0 and invoice_id > 0:
            tx_check = await db.execute(select(TransactionDB).where(TransactionDB.invoice_id == invoice_id))
            if tx_check.scalars().first():
                return {"ok": True, "message": "Already processed"}

            new_tx = TransactionDB(invoice_id=invoice_id, user_id=user_id, amount=amount_usd)
            db.add(new_tx)

            user_res = await db.execute(select(UserDB).where(UserDB.user_id == user_id))
            user = user_res.scalars().first()

            if user:
                user.balance += amount_usd
            else:
                user = UserDB(user_id=user_id, balance=amount_usd)
                db.add(user)

            await db.commit()

            try:
                await bot.send_message(
                    user_id,
                    f"✅ <b>Баланс успешно пополнен!</b>\nЗачислено: <b>${amount_usd:.2f}</b> 💎",
                    parse_mode="HTML"
                )
            except Exception as err:
                logging.warning(f"Ошибка отправки сообщения пользователю {user_id}: {err}")

    return {"ok": True}


@app.post("/admin/set-balance")
async def admin_set_balance(data: AdminActionRequest, db: AsyncSession = Depends(get_db)):
    if data.admin_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Access denied")

    res = await db.execute(select(UserDB).where(UserDB.user_id == data.target_user_id))
    user = res.scalars().first()

    if not user:
        user = UserDB(user_id=data.target_user_id, balance=data.amount)
        db.add(user)
    else:
        user.balance = data.amount

    await db.commit()
    return {"status": "success", "new_balance": user.balance}


# --- 7. ХЕНДЛЕРЫ ТЕЛЕГРАМ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Открыть Almaz Shop",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )

    if message.from_user.id == ADMIN_USER_ID:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")
        ])

    welcome_text = (
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Добро пожаловать в <b>Almaz Shop</b> — ваш надежный магазин!\n\n"
        "Нажмите на кнопку ниже, чтобы открыть приложение:"
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "admin_panel")
async def process_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        users_res = await session.execute(select(UserDB))
        all_users = users_res.scalars().all()

        admin_res = await session.execute(select(UserDB).where(UserDB.user_id == ADMIN_USER_ID))
        admin_user = admin_res.scalars().first()
        admin_balance = admin_user.balance if admin_user else 0.0

        total_users = len(all_users)
        total_balance = sum(u.balance for u in all_users)

    admin_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Выдать себе $10", callback_data="add_self_10"),
                InlineKeyboardButton(text="➕ Выдать себе $100", callback_data="add_self_100")
            ],
            [
                InlineKeyboardButton(text="✏️ Изменить баланс пользователю", callback_data="admin_give_user")
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="admin_panel")
            ]
        ]
    )

    stats_text = (
        "⚙️ <b>Панель администратора Almaz Shop</b>\n\n"
        f"👑 Ваш личный баланс: <b>${admin_balance:.2f} USDT</b>\n"
        f"👥 Всего пользователей в системе: <b>{total_users}</b>\n"
        f"💰 Общая сумма на балансах: <b>${total_balance:.2f}</b>\n\n"
        "Выберите действие ниже:"
    )

    try:
        await callback.message.edit_text(stats_text, parse_mode="HTML", reply_markup=admin_keyboard)
    except Exception:
        await callback.message.answer(stats_text, parse_mode="HTML", reply_markup=admin_keyboard)

    await callback.answer()


@dp.callback_query(lambda c: c.data in ["add_self_10", "add_self_100"])
async def process_add_self_balance(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    add_amount = 10.0 if callback.data == "add_self_10" else 100.0

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(UserDB).where(UserDB.user_id == ADMIN_USER_ID))
        user = res.scalars().first()

        if not user:
            user = UserDB(user_id=ADMIN_USER_ID, balance=add_amount)
            session.add(user)
        else:
            user.balance += add_amount

        await session.commit()

    await callback.answer(f"✅ Зачислено +${add_amount:.0f} USDT!", show_alert=True)
    await process_admin_panel(callback)


@dp.callback_query(lambda c: c.data == "admin_give_user")
async def process_start_give_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.message.answer("Введите **Telegram ID** пользователя, которому нужно изменить баланс:")
    await callback.answer()


@dp.message(AdminStates.waiting_for_user_id)
async def process_input_user_id(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return

    if not message.text.isdigit():
        await message.answer("❌ ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    await state.update_data(target_user_id=int(message.text))
    await state.set_state(AdminStates.waiting_for_amount)
    await message.answer("Введите **новую сумму баланса** в USDT (например: 50 или 100.5):")


@dp.message(AdminStates.waiting_for_amount)
async def process_input_amount(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return

    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректное число (например: 25 или 100.5):")
        return

    data = await state.get_data()
    target_id = data["target_user_id"]

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(UserDB).where(UserDB.user_id == target_id))
        user = res.scalars().first()

        if not user:
            user = UserDB(user_id=target_id, balance=amount)
            session.add(user)
        else:
            user.balance = amount

        await session.commit()

    await state.clear()
    await message.answer(f"✅ Баланс пользователя <code>{target_id}</code> изменен на **${amount:.2f} USDT**!", parse_mode="HTML")
