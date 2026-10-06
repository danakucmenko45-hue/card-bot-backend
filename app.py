import os
import hmac
import hashlib
import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from sqlalchemy import create_engine, Column, Float, String, BigInteger, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# --- 1. НАСТРОЙКИ ---
TELEGRAM_TOKEN = "8983015392:AAEP4SykIhK_TpwPLLzRNi-2-K4sEHMbRco"
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"
WEBAPP_URL = "https://almaz-shop-mini-app-59h1.vercel.app"
ADMIN_USER_ID = 7334078827

# Курс конвертации: Сколько Звёзд даётся за 1 USDT (50 Stars = 1.00$)
STARS_PER_USDT = 50

logging.basicConfig(level=logging.INFO)

# --- 2. БАЗА ДАННЫХ ---
DATABASE_URL = "sqlite:///./almaz_shop.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserDB(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True, index=True)
    balance = Column(Float, default=0.0)


class TransactionDB(Base):
    __tablename__ = "transactions"
    invoice_id = Column(String, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    processed = Column(Boolean, default=True)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- 3. ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()


# --- 4. FASTAPI С LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task = asyncio.create_task(dp.start_polling(bot))
    logging.info("✅ Бот и платежный сервер Almaz Shop успешно запущены!")

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


class CheckInvoiceRequest(BaseModel):
    invoice_id: int
    user_id: int


class StarsInvoiceRequest(BaseModel):
    amount_usd: float
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
async def get_balance(user_id: int, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.user_id == user_id).first()
    if not user:
        user = UserDB(user_id=user_id, balance=0.0)
        db.add(user)
        db.commit()
        db.refresh(user)

    return {"balance": user.balance}


# Создание счета CryptoBot (Минимум $12)
@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
    if data.amount < 12.0:
        raise HTTPException(status_code=400, detail="Минимальная сумма пополнения: $12")

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


# Моментальная проверка оплаты (вызывается из WebApp при нажатии кнопки проверки)
@app.post("/check-invoice")
async def check_invoice(data: CheckInvoiceRequest, db: Session = Depends(get_db)):
    import httpx
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": data.invoice_id}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params, headers=headers)
            res_data = response.json()
    except Exception as e:
        logging.error(f"CryptoBot Check Error: {e}")
        raise HTTPException(status_code=500, detail="Ошибка связи с платежной системой")

    if not res_data.get("ok") or not res_data.get("result", {}).get("items"):
        raise HTTPException(status_code=404, detail="Счет не найден")

    invoice_info = res_data["result"]["items"][0]
    status = invoice_info.get("status") # active / paid / expired
    amount_usd = float(invoice_info.get("amount", 0.0))
    str_invoice_id = str(data.invoice_id)

    if status == "paid":
        # Проверяем, не был ли этот счет уже засчитан ранее
        tx_check = db.query(TransactionDB).filter(TransactionDB.invoice_id == str_invoice_id).first()
        if not tx_check:
            # Записываем транзакцию
            new_tx = TransactionDB(invoice_id=str_invoice_id, user_id=data.user_id, amount=amount_usd)
            db.add(new_tx)

            # Начисляем баланс
            user = db.query(UserDB).filter(UserDB.user_id == data.user_id).first()
            if user:
                user.balance += amount_usd
            else:
                user = UserDB(user_id=data.user_id, balance=amount_usd)
                db.add(user)

            db.commit()

            # Уведомляем пользователя в боте
            try:
                await bot.send_message(
                    data.user_id,
                    f"✅ <b>Оплата успешно подтверждена!</b>\nЗачислено на баланс: <b>${amount_usd:.2f} USDT</b> 💎",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        return {"status": "paid", "amount": amount_usd}
    
    elif status == "expired":
        return {"status": "expired"}
    
    return {"status": "active"}


# Пополнение через Telegram Stars (Звёзды)
@app.post("/create-stars-invoice")
async def create_stars_invoice(data: StarsInvoiceRequest):
    if data.amount_usd < 0.5:
        raise HTTPException(status_code=400, detail="Минимальная сумма пополнения: $0.50")

    stars_count = int(data.amount_usd * STARS_PER_USDT)
    if stars_count < 1:
        stars_count = 1

    try:
        invoice_link = await bot.create_invoice_link(
            title="Пополнение баланса Almaz Shop",
            description=f"Пополнение личного счета на ${data.amount_usd:.2f} USDT ({stars_count} ⭐️)",
            payload=f"stars_{data.user_id}_{data.amount_usd}",
            currency="XTR",
            prices=[LabeledPrice(label="Пополнение USDT", amount=stars_count)]
        )
        return {"pay_url": invoice_link, "stars_amount": stars_count}
    except Exception as e:
        logging.error(f"Ошибка создания Stars счета: {e}")
        raise HTTPException(status_code=500, detail="Ошибка генерации счета Telegram Stars")


# Резервный вебхук CryptoBot (на случай задержек сети)
@app.post("/crypto-webhook")
async def crypto_webhook(
    request: Request,
    crypto_pay_api_signature: str = Header(None),
    db: Session = Depends(get_db)
):
    body_bytes = await request.body()

    if CRYPTO_BOT_TOKEN and crypto_pay_api_signature:
        secret = hashlib.sha256(CRYPTO_BOT_TOKEN.encode()).digest()
        check_signature = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        if check_signature != crypto_pay_api_signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

    update = await request.json()

    if update.get("update_type") == "invoice_paid":
        payload_data = update.get("payload", {})
        invoice_id = str(payload_data.get("invoice_id", "0"))
        user_id = int(payload_data.get("payload", 0))
        amount_usd = float(payload_data.get("amount", 0.0))

        if user_id > 0 and invoice_id != "0":
            tx_check = db.query(TransactionDB).filter(TransactionDB.invoice_id == invoice_id).first()
            if not tx_check:
                new_tx = TransactionDB(invoice_id=invoice_id, user_id=user_id, amount=amount_usd)
                db.add(new_tx)

                user = db.query(UserDB).filter(UserDB.user_id == user_id).first()
                if user:
                    user.balance += amount_usd
                else:
                    user = UserDB(user_id=user_id, balance=amount_usd)
                    db.add(user)

                db.commit()

                try:
                    await bot.send_message(
                        user_id,
                        f"✅ <b>Баланс успешно пополнен через CryptoBot!</b>\nЗачислено: <b>${amount_usd:.2f} USDT</b> 💎",
                        parse_mode="HTML"
                    )
                except Exception as err:
                    logging.warning(f"Ошибка отправки сообщения пользователю {user_id}: {err}")

    return {"ok": True}


# Изменение баланса через админку API
@app.post("/admin/set-balance")
async def admin_set_balance(data: AdminActionRequest, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Access denied")

    user = db.query(UserDB).filter(UserDB.user_id == data.target_user_id).first()
    if not user:
        user = UserDB(user_id=data.target_user_id, balance=data.amount)
        db.add(user)
    else:
        user.balance = data.amount

    db.commit()
    return {"status": "success", "new_balance": user.balance}


# --- 7. ОБРАБОТКА ОПЛАТЫ TELEGRAM STARS ---
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload

    if payload.startswith("stars_"):
        parts = payload.split("_")
        user_id = int(parts[1])
        amount_usd = float(parts[2])
        charge_id = payment.telegram_payment_charge_id

        db = SessionLocal()
        try:
            tx_check = db.query(TransactionDB).filter(TransactionDB.invoice_id == charge_id).first()
            if not tx_check:
                new_tx = TransactionDB(invoice_id=charge_id, user_id=user_id, amount=amount_usd)
                db.add(new_tx)

                user = db.query(UserDB).filter(UserDB.user_id == user_id).first()
                if user:
                    user.balance += amount_usd
                else:
                    user = UserDB(user_id=user_id, balance=amount_usd)
                    db.add(user)

                db.commit()

                await message.answer(
                    f"🌟 <b>Оплата Telegram Stars прошла успешно!</b>\n\n"
                    f"Списано: <b>{payment.total_amount} ⭐️</b>\n"
                    f"Зачислено на счет: <b>${amount_usd:.2f} USDT</b> 💎",
                    parse_mode="HTML"
                )

                await bot.send_message(
                    ADMIN_USER_ID,
                    f"💰 <b>Новое пополнение Stars!</b>\n\n"
                    f"Пользователь: <code>{user_id}</code>\n"
                    f"Получено: <b>{payment.total_amount} Stars ⭐️</b>\n"
                    f"Зачислено клиенту: <b>${amount_usd:.2f} USDT</b>",
                    parse_mode="HTML"
                )
        finally:
            db.close()


# --- 8. ХЕНДЛЕРЫ ТЕЛЕГРАМ БОТА ---
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

    db = SessionLocal()
    try:
        all_users = db.query(UserDB).all()
        admin_user = db.query(UserDB).filter(UserDB.user_id == ADMIN_USER_ID).first()
        admin_balance = admin_user.balance if admin_user else 0.0

        total_users = len(all_users)
        total_balance = sum(u.balance for u in all_users)
    finally:
        db.close()

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

    db = SessionLocal()
    try:
        user = db.query(UserDB).filter(UserDB.user_id == ADMIN_USER_ID).first()
        if not user:
            user = UserDB(user_id=ADMIN_USER_ID, balance=add_amount)
            db.add(user)
        else:
            user.balance += add_amount
        db.commit()
    finally:
        db.close()

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
async def process_input_amount(message: types.Message, state: `FSMContext`): # type: ignore
    if message.from_user.id != ADMIN_USER_ID:
        return

    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректное число (например: 25 или 100.5):")
        return

    data = await state.get_data()
    target_id = data["target_user_id"]

    db = SessionLocal()
    try:
        user = db.query(UserDB).filter(UserDB.user_id == target_id).first()
        if not user:
            user = UserDB(user_id=target_id, balance=amount)
            db.add(user)
        else:
            user.balance = amount
        db.commit()
    finally:
        db.close()

    await state.clear()
    await message.answer(f"✅ Баланс пользователя <code>{target_id}</code> изменен на **${amount:.2f} USDT**!", parse_mode="HTML")
