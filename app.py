import re
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiocryptopay import CryptoPay, Networks

app = FastAPI(title="Crystal Shop Backend")

# 1. Настройка CORS для Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Разрешает запросы с вашего сайта на Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Токен Crypto Pay API
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"

# Инициализация клиента Crypto Pay
crypto = CryptoPay(token=CRYPTO_BOT_TOKEN, network=Networks.MAINNET)

# Временная база данных балансов в памяти
user_balances = {}

class InvoiceRequest(BaseModel):
    amount: float
    user_id: int

@app.get("/")
async def root():
    return {"status": "ok", "message": "Crystal Shop Backend is running!"}

@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
    # Минимальная сумма пополнения — $15
    if data.amount < 15.0:
        raise HTTPException(status_code=400, detail="Minimum deposit amount is $15")

    try:
        # Создаем счет в USDT
        invoice = await crypto.create_invoice(
            asset='USDT',
            amount=data.amount,
            payload=str(data.user_id)
        )
        
        raw_url = invoice.bot_invoice_url
        
        # Исправление ссылки для идеального открытия внутри Telegram WebApp
        if "t.me/CryptoPayBot" in raw_url:
            pay_url = raw_url.replace("t.me/CryptoPayBot", "t.me/CryptoBot")
        else:
            pay_url = raw_url

        return {
            "pay_url": pay_url, 
            "invoice_id": invoice.invoice_id
        }
    except Exception as e:
        print(f"❌ Ошибка создания счета: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/crypto-webhook")
async def crypto_webhook(request: Request):
    try:
        update = await request.json()
        
        # Начисление баланса при успешной оплате
        if update.get("update_type") == "invoice_paid":
            payload_data = update.get("payload", {})
            user_id = int(payload_data.get("payload", 0))
            amount_usd = float(payload_data.get("amount", 0.0))

            if user_id > 0:
                user_balances[user_id] = user_balances.get(user_id, 0.0) + amount_usd
                print(f"✅ Баланс успешно пополнен: User {user_id} +${amount_usd}")

        return {"ok": True}
    except Exception as e:
        print(f"❌ Ошибка вебхука: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int):
    return {"balance": user_balances.get(user_id, 0.0)}
