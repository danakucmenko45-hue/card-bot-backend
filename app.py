RuntimeError: There is no current event loop in thread 'MainThread'.
```[cite: 10]

### Что произошло?
В Python версии 3.14 (которую автоматически установил Render[cite: 10]) асинхронный цикл событий (`asyncio event loop`) **не создается автоматически на глобальном уровне**, когда модуль загружается через Uvicorn[cite: 10]. 

При создании объекта `crypto = AioCryptoPay(...)` прямо в глобальном пространстве `app.py`, библиотека пытается получить существующий `event_loop`, которого еще нет, и сервер моментально падает с `RuntimeError`[cite: 10].

---

### Как исправить (100% рабочее решение):

Инициализацию `AioCryptoPay` нужно перенести **внутрь события запуска FastAPI (Lifespan)**, когда асинхронный цикл `asyncio` уже гарантированно активен.

Замените весь код в файле **`app.py`** на GitHub на этот готовый вариант:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiocryptopay import AioCryptoPay

# Токен Crypto Pay API
CRYPTO_BOT_TOKEN = "641830:AApeUWiszQ46wcy6juCxVp5F4unJUqZfm9I"

# Глобальный клиент (инициализируется при старте сервера)
crypto = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global crypto
    # Инициализируем Crypto Pay ВНУТРИ активного asyncio event loop
    crypto = AioCryptoPay(token=CRYPTO_BOT_TOKEN)
    yield
    # Корректно закрываем сессию при остановке
    if crypto:
        await crypto.close()

app = FastAPI(title="Crystal Shop Backend", lifespan=lifespan)

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

user_balances = {}

class InvoiceRequest(BaseModel):
    amount: float
    user_id: int

@app.get("/")
async def root():
    return {"status": "ok", "message": "Backend is running!"}

@app.post("/create-invoice")
async def create_invoice(data: InvoiceRequest):
    if data.amount < 15.0:
        raise HTTPException(status_code=400, detail="Minimum deposit is $15")

    try:
        invoice = await crypto.create_invoice(
            asset='USDT',
            amount=data.amount,
            payload=str(data.user_id)
        )
        
        raw_url = invoice.bot_invoice_url
        
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
        
        if update.get("update_type") == "invoice_paid":
            payload_data = update.get("payload", {})
            user_id = int(payload_data.get("payload", 0))
            amount_usd = float(payload_data.get("amount", 0.0))

            if user_id > 0:
                user_balances[user_id] = user_balances.get(user_id, 0.0) + amount_usd
                print(f"✅ Баланс пополнен: User {user_id} +${amount_usd}")

        return {"ok": True}
    except Exception as e:
        print(f"❌ Ошибка вебхука: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/get-balance/{user_id}")
async def get_balance(user_id: int):
    return {"balance": user_balances.get(user_id, 0.0)}
