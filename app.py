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

from sqlalchemy import Column, Float, String, BigInteger, Boolean
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.future import select

# --- НАСТРОЙКИ С ВАШИМИ ДАННЫМИ ---
TELEGRAM_TOKEN = "8983015392:AAEP4SykIhK_TpwPLLzRNi-2-K4sEHMbRco"
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"
WEBAPP_URL = "https://almaz-shop.vercel.app"
ADMIN_USER_ID = 7334078827

logging.basicConfig(level=logging.INFO)

# Настройка асинхронной базы данных SQLite
DATABASE_URL = "sqlite+aiosqlite:///./almaz_shop.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()


# --- МОДЕЛИ БАЗЫ ДАННЫХ ---
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


# Зависимость для получения асинхронной сессии БД
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- ИНИЦИАЛИЗАЦИЯ БОТА И FASTAPI ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создание таблиц при старте
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Запуск поллинга бота
    polling_task = asyncio.create_task(dp.start_polling(bot))
    logging.info("✅ База данных подключена, Telegram бот запущен!")

    yield

    # Остановка бота при завершении работы приложения
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


# --- PYDANTIC СХЕМЫ ---
class InvoiceRequest(BaseModel):
    amount: float
    user_id: int


class AdminActionRequest(BaseModel):
    admin_id: int
    target_user_id: int
    amount: float


# --- ЭНДПОИНТЫ API ---
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

    # Проверка подписи от CryptoBot
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
            # Проверка, не обрабатывался ли этот счет ранее
            tx_check = await db.execute(select(TransactionDB).where(TransactionDB.invoice_id == invoice_id))
            if tx_check.scalars().first():
                return {"ok": True, "message": "Already processed"}

            # Запись транзакции
            new_tx = TransactionDB(invoice_id=invoice_id, user_id=user_id, amount=amount_usd)
            db.add(new_tx)

            # Начисление баланса
            user_res = await db.execute(select(UserDB).where(UserDB.user_id == user_id))
            user = user_res.scalars().first()

            if user:
                user.balance += amount_usd
            else:
