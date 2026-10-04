import logging
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import create_engine, Column, Integer, Float, String, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

TELEGRAM_TOKEN = "8983015392:AAEP4SykIhK_TpwPLLzRNi-2-K4sEHMbRco"
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"
WEBAPP_URL = "https://almaz-shop.vercel.app"
ADMIN_USER_ID = 7334078827  # ЗАМЕНИТЕ НА СВОЙ TELEGRAM ID ДЛЯ ДОСТУПА В АДМИНКУ

logging.basicConfig(level=logging.INFO)

# Настройка базы данных SQLite
DATABASE_URL = "sqlite:///./almaz_shop.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Модель пользователя в базе данных
class UserDB(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True, index=True)
    balance = Column(Float, default=0.0)

Base.metadata.create_all(bind=engine)

# Зависимость для получения сессии БД
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI(title="Almaz Shop Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

class InvoiceRequest(BaseModel):
    amount: float
    user_id: int

class AdminActionRequest(BaseModel):
    admin_id: int
    target_user_id: int
    amount: float

@app.get("/")
async def root():
    return {"status": "ok", "message": "Almaz Shop Backend is active with Database!"}

@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.user_id == user_id).first()
    if not user:
        # Создаем пользователя, если его еще нет в базе
        user = UserDB(user_id=user_id, balance=0.0)
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"balance": user.balance}

@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest, db: Session = Depends(get_db)):
    if data.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum deposit is $1")

    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(data.amount),
        "payload": str(data.user_id)
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            res_data = response.json()
    except Exception as e:
        logging.error(f"CryptoBot connection error: {e}")
        raise HTTPException(status_code=500, detail="Payment gateway timeout.")

    if not res_data.get("ok"):
        raise HTTPException(status_code=500, detail="CryptoBot API Error")

    result = res_data["result"]
    raw_url = result.get("pay_url") or result.get("bot_invoice_url", "")
    pay_url = raw_url.replace("t.me/CryptoPayBot", "t.me/CryptoBot")

    return {
        "pay_url": pay_url,
        "invoice_id": result.get("invoice_id")
    }

@app.post("/crypto-webhook")
async def crypto_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        update = await request.json()
        if update.get("update_type") == "invoice_paid":
            payload_data = update.get("payload", {})
            user_id = int(payload_data.get("payload", 0))
            amount_usd = float(payload_data.get("amount", 0.0))

            if user_id > 0:
                user = db.query(UserDB).filter(UserDB.user_id == user_id).first()
                if user:
                    user.balance += amount_usd
                else:
                    user = UserDB(user_id=user_id, balance=amount_usd)
                    db.add(user)
                db.commit()

                # Автоматическое уведомление в Telegram о пополнении
                try:
                    await bot.send_message(
                        user_id, 
                        f"✅ <b>Баланс успешно пополнен!</b>\nЗачислено: <b>${amount_usd:.2f}</b> 💎", 
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --- АДМИН-ПАНЕЛЬ (Эндпоинты) ---
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

@app.get("/admin/stats/{admin_id}")
async def admin_stats(admin_id: int, db: Session = Depends(get_db)):
    if admin_id != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Access denied")
    
    total_users = db.query(UserDB).count()
    all_users = db.query(UserDB).all()
    total_balance = sum(u.balance for u in all_users)
    
    return {
        "total_users": total_users,
        "total_balance_in_system": total_balance
    }

# --- ТЕЛЕГРАМ БОТ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
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
    
    # Если админ пишет /start, можно дополнительно выводить статус
    if message.from_user.id == ADMIN_USER_ID:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")
        ])

    welcome_text = (
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Добро пожаловать в <b>Almaz Shop</b> — лучший магазин виртуальных карт!\n\n"
        "Нажмите на кнопку ниже, чтобы открыть магазин:"
    )
    
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=keyboard)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))
    logging.info("Database and Telegram bot started successfully!")
