import os
import asyncio
import random
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from fastapi import FastAPI
import uvicorn

BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
app = FastAPI()

# База данных пользователей в памяти
users = {}

def generate_card():
    number = "4" + "".join([str(random.randint(0, 9)) for _ in range(15)])
    exp = f"{random.randint(1, 12):02d}/{random.randint(26, 30)}"
    cvc = "".join([str(random.randint(0, 9)) for _ in range(3)])
    return f"💳 **Карта выкуплена!**\nНомер: `{number}`\nСрок: `{exp}`\nCVC: `{cvc}`"

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Привет! Открывай наш Mini App через синюю кнопку внизу слева, чтобы выбрать и купить карту!")

@dp.message(lambda msg: msg.web_app_data is not None)
async def web_app_handler(message: types.Message):
    # Генерация реквизитов при покупке из Mini App
    card_info = generate_card()
    await message.answer(card_info, parse_mode="Markdown")

@app.get("/")
async def root():
    return {"status": "Backend running"}

async def main():
    if bot:
        asyncio.create_task(dp.start_polling(bot))

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(main())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)