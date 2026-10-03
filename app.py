from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiocryptopay import CryptoPay, Networks

app = FastAPI()

# Разрешаем веб-приложению обращаться к бэкенду
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Токен Crypto Pay API
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"

# Инициализация Crypto Pay
crypto = CryptoPay(token=CRYPTO_BOT_TOKEN, network=Networks.MAINNET)

# База данных балансов пользователей
user_balances = {}

class InvoiceRequest(BaseModel):
    amount: float
    user_id: int

@app.get("/")
async def root():
    return {"status": "ok", "message": "Backend is running!"}

@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
    # Проверка минимальной суммы $15
    if data.amount < 15.0:
        raise HTTPException(status_code=400, detail="Minimum deposit amount is $15")

    try:
        # Создание счета на оплату в USDT
        invoice = await crypto.create_invoice(
            asset='USDT',
            amount=data.amount,
            payload=str(data.user_id)
        )
        return {"pay_url": invoice.bot_invoice_url, "invoice_id": invoice.invoice_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/crypto-webhook")
async def crypto_webhook(request: Request):
    update = await request.json()
    
    # Автоматическое начисление денег при успешной оплате
    if update.get("update_type") == "invoice_paid":
        payload = update["payload"]
        user_id = int(payload["payload"])
        amount_usd = float(payload["amount"])

        user_balances[user_id] = user_balances.get(user_id, 0.0) + amount_usd
        print(f"✅ Успешное пополнение: Пользователь {user_id} +${amount_usd}")

    return {"ok": True}

@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int):
    return {"balance": user_balances.get(user_id, 0.0)}
