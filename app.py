from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Определение состояний для ввода ID и суммы при выдаче баланса другому человеку
class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_amount = State()

# --- ХЕНДЛЕРЫ ТЕЛЕГРАМ БОТА ---

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

    # Кнопка админ-панели видна ТОЛЬКО владельцу (ADMIN_USER_ID)
    if message.from_user.id == ADMIN_USER_ID:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="⚙️️ Админ-панель", callback_data="admin_panel")
        ])

    welcome_text = (
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Добро пожаловать в <b>Almaz Shop</b> — ваш надежный магазин!\n\n"
        "Нажмите на кнопку ниже, чтобы открыть приложение:"
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=keyboard)


# Вызов админ-панели (Строгая проверка ID)
@dp.callback_query(lambda c: c.data == "admin_panel")
async def process_admin_panel(callback: types.CallbackQuery):
    # Защита: Если ID не совпадает с ADMIN_USER_ID, доступ блокируется
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("⛔ Доступ запрещен! Вы не являетесь администратором.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        users_res = await session.execute(select(UserDB))
        all_users = users_res.scalars().all()
        
        # Получаем текущий баланс самого админа
        admin_res = await session.execute(select(UserDB).where(UserDB.user_id == ADMIN_USER_ID))
        admin_user = admin_res.scalars().first()
        admin_balance = admin_user.balance if admin_user else 0.0

        total_users = len(all_users)
        total_balance = sum(u.balance for u in all_users)

    # Интерактивные кнопки админ-панели
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
        "⚙️️ <b>Панель администратора Almaz Shop</b>\n\n"
        f"👑 Ваш личный баланс: <b>${admin_balance:.2f} USDT</b>\n"
        f"👥 Всего пользователей в системе: <b>{total_users}</b>\n"
        f"💰 Общая сумма на балансах: <b>${total_balance:.2f}</b>\n\n"
        "Выберите действие ниже:"
    )

    await callback.message.edit_text(stats_text, parse_mode="HTML", reply_markup=admin_keyboard)
    await callback.answer()


# Быстрая выдача баланса самому себе (+10 или +100 USDT)
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
        new_balance = user.balance

    await callback.answer(f"✅ Успешно зачислено +${add_amount:.0f} USDT!", show_alert=True)
    
    # Обновляем текст панели
    await process_admin_panel(callback)


# Выдача произвольной суммы пользователю по ID
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
    await message.answer("Введите **новую сумму баланса** в USDT (например: 50.5):")


@dp.message(AdminStates.waiting_for_amount)
async def process_input_amount(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return

    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Некорректная сумма. Введите число (например: 25 или 100.5):")
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
    await message.answer(f"✅ Баланс пользователя <code>{target_id}</code> успешно изменен на **${amount:.2f} USDT**!", parse_mode="HTML")
