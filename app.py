import logging
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

TELEGRAM_TOKEN = "8983015392:AAEP4SykIhK_TpwPLLzRNi-2-K4sEHMbRco"
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"
WEBAPP_URL = "https://almaz-shop.vercel.app"

logging.basicConfig(level=logging.INFO)

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

user_balances = {}

class InvoiceRequest(BaseModel):
    amount: float
    user_id: int

@app.get("/")
async def root():
    return {"status": "ok", "message": "Almaz Shop Backend is active!"}

@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
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
        # Увеличенный таймаут (30 секунд), чтобы запрос не обрывался при пробуждении сервера
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            res_data = response.json()
    except Exception as e:
        logging.error(f"CryptoBot connection error: {e}")
        raise HTTPException(status_code=500, detail="Payment gateway timeout, please try again.")

    if not res_data.get("ok"):
        logging.error(f"CryptoBot API returned error: {res_data}")
        raise HTTPException(status_code=500, detail="CryptoBot API Error")

    result = res_data["result"]
    raw_url = result.get("pay_url") or result.get("bot_invoice_url", "")
    pay_url = raw_url.replace("t.me/CryptoPayBot", "t.me/CryptoBot")

    return {
        "pay_url": pay_url,
        "invoice_id": result.get("invoice_id")
    }

@app.post("/crypto-webhook")
async def crypto_webhook(request: Request):
    try:
        update = await request.json()
        if update.get("update_type") == "invoice_paid":
            payload_data = update.get("payload", {})
            user_id = int(payload_data.get("payload", 0))
            amount_usd = float(payload_data.get("amount", 0.0))

            if user_id > 0:
                user_balances[user_id] = user_balances.get(user_id, 0.00) + amount_usd

        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int):
    return {"balance": user_balances.get(user_id, 0.00)}

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
    
    welcome_text = (
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Нажмите на кнопку ниже, чтобы открыть магазин:"
    )
    
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=keyboard)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))
    logging.info("Telegram bot and FastAPI server started successfully!")
