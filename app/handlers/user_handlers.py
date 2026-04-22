"""Обработчики пользовательских команд — /start, меню, профиль и т.д."""

import logging
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import ADMIN_IDS, REFERRAL_BONUS
from app.database import get_db
from app.states import UserStates
from app.services.loyalty import LoyaltySystem, LoyaltyManager
from app.services.analytics import MLRecommendationEngine
from app.keyboards.main import (
    get_main_menu_keyboard,
    get_diet_filter_keyboard,
    get_back_keyboard,
)
from app.utils.safe_edit import safe_edit_text, safe_answer_callback
from app.utils.validators import (
    validate_phone,
    validate_email,
    validate_birth_date,
    format_phone,
)

logger = logging.getLogger(__name__)
user_router = Router()


@user_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Команда /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    # Проверка реферального кода
    referral_code = None
    parts = message.text.split()
    if len(parts) > 1:
        ref = parts[1]
        if ref.startswith("ref"):
            referral_code = ref[3:]

    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            exists = await cursor.fetchone()

            if not exists:
                ref_code = LoyaltySystem.generate_referral_code(user_id)

                await db.execute(
                    "INSERT INTO users (user_id, username, first_name, registered_at, referral_code, referred_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, username, first_name, datetime.now().isoformat(), ref_code, referral_code)
                )
                await db.execute("INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)", (user_id,))

                # Приветственные баллы
                await db.execute(
                    "UPDATE users SET loyalty_points = loyalty_points + 100 WHERE user_id = ?",
                    (user_id,)
                )

                # Обработка реферала
                if referral_code:
                    lm = LoyaltyManager(db)
                    await lm.process_referral(user_id, referral_code)

                welcome_msg = (
                    f"🎉 Добро пожаловать, {first_name}!\n\n"
                    f"Вам начислено 100 приветственных баллов!\n"
                    f"Ваш реферальный код: <code>{ref_code}</code>\n"
                    f"Приглашайте друзей и получайте по {REFERRAL_BONUS} баллов!"
                )
                logger.info(f"Новый пользователь: {user_id} (@{username})")
            else:
                welcome_msg = f"👋 С возвращением, {first_name}!"

            # Проверка ДР
            lm = LoyaltyManager(db)
            birthday_discount = await lm.check_birthday(user_id)
            if birthday_discount:
                welcome_msg += f"\n\n🎂 С Днем Рождения! Скидка {birthday_discount}% на сегодня!"

        is_admin = user_id in ADMIN_IDS
        cart_count = await _get_cart_count(user_id)

        await message.answer(
            welcome_msg,
            reply_markup=get_main_menu_keyboard(is_admin, cart_count > 0),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка в cmd_start для пользователя {user_id}: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке команды. Попробуйте позже.")
        return


@user_router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, state: FSMContext):
    """Показать меню"""
    user_id = callback.from_user.id
    data = await state.get_data()
    diet_filters = data.get("diet_filters", [])

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT diet_type, allergens FROM users WHERE user_id = ?", (user_id,)
        )
        user_prefs = await cursor.fetchone()

        query = "SELECT * FROM menu WHERE is_active = 1"
        params = []

        if diet_filters:
            conditions = [f"diet_tags LIKE ?" for _ in diet_filters]
            params = [f'%"{d}"%' for d in diet_filters]
            query += " AND (" + " OR ".join(conditions) + ")"

        if user_prefs and user_prefs[1]:
            try:
                allergens = json.loads(user_prefs[1])
                for allergen in allergens:
                    query += " AND allergens NOT LIKE ?"
                    params.append(f'%"{allergen}"%')
            except json.JSONDecodeError:
                pass

        query += " ORDER BY popularity_score DESC"
        cursor = await db.execute(query, params)
        dishes = await cursor.fetchall()

    if not dishes:
        text = "🍽️ <b>Меню</b>\n\nК сожалению, по вашим фильтрам ничего не найдено."
    else:
        text = "🍽️ <b>Наше меню</b>\n\n"
        for dish in dishes:
            diet_icons = ""
            if dish[10]:
                try:
                    tags = json.loads(dish[10])
                    icons = {
                        "vegan": "🌱",
                        "vegetarian": "🥗",
                        "gluten_free": "🌾",
                        "dairy_free": "🥛",
                        "nut_free": "🥜",
                    }
                    diet_icons = "".join(icons.get(t, "") for t in tags)
                except json.JSONDecodeError:
                    pass

            text += f"{diet_icons} <b>{dish[1]}</b> — {dish[3]}₽\n"
            text += f"   ⭐ {dish[12] if len(dish) > 12 else 5.0}/5 | ⏱️ {dish[8] if len(dish) > 8 else '?'}мин\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Фильтры", callback_data="diet_filter")

    for dish in dishes[:10]:
        builder.button(text=f"➕ {dish[1][:25]}", callback_data=f"dish_{dish[0]}")

    cart_count = await _get_cart_count(user_id)
    if cart_count > 0:
        builder.button(text=f"🛒 Корзина ({cart_count})", callback_data="cart")

    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(1)

    await safe_edit_text(
        callback,
        text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "diet_filter")
async def show_diet_filter(callback: CallbackQuery, state: FSMContext):
    """Фильтр по диетам"""
    data = await state.get_data()
    selected = data.get("diet_filters", [])

    await safe_edit_text(
        callback,
        "🔍 <b>Фильтр по диетам</b>\n\nВыберите предпочтения:",
        reply_markup=get_diet_filter_keyboard(selected),
        parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data.startswith("diet_toggle_"))
async def toggle_diet(callback: CallbackQuery, state: FSMContext):
    """Переключение фильтра диеты"""
    diet = callback.data.split("_")[2]
    data = await state.get_data()
    selected = list(data.get("diet_filters", []))

    if diet in selected:
        selected.remove(diet)
    else:
        selected.append(diet)

    await state.update_data(diet_filters=selected)
    new_keyboard = get_diet_filter_keyboard(selected)

    await safe_edit_text(
        callback,
        "🔍 Фильтр обновлён",
        reply_markup=new_keyboard
    )


@user_router.callback_query(F.data == "diet_apply")
async def apply_diet_filter(callback: CallbackQuery, state: FSMContext):
    """Применить фильтр"""
    await show_menu(callback, state)


@user_router.callback_query(F.data == "diet_reset")
async def reset_diet_filter(callback: CallbackQuery, state: FSMContext):
    """Сбросить фильтр"""
    await state.update_data(diet_filters=[])
    await show_menu(callback, state)


@user_router.callback_query(F.data == "recommendations")
async def show_recommendations(callback: CallbackQuery):
    """Персональные рекомендации"""
    user_id = callback.from_user.id

    async with get_db() as db:
        ml = MLRecommendationEngine(db)
        recommendations = await ml.get_recommendations(user_id, 5)

    if not recommendations:
        await safe_answer_callback(callback, "Пока нет рекомендаций", show_alert=True)
        return

    text = "🤖 <b>Персональные рекомендации</b>\n\n"
    for i, dish in enumerate(recommendations, 1):
        text += f"{i}. <b>{dish['name']}</b>\n"
        text += f"   ⭐ {dish['rating']} | 💰 {dish['price']}₽\n"
        if dish.get("diet_tags"):
            text += f"   🏷️ {', '.join(dish['diet_tags'])}\n"
        text += "\n"

    builder = InlineKeyboardBuilder()
    for dish in recommendations:
        builder.button(
            text=f"➕ {dish['name'][:20]}", callback_data=f"dish_{dish['id']}"
        )
    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(1)

    await safe_edit_text(
        callback,
        text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "loyalty")
async def show_loyalty(callback: CallbackQuery):
    """Программа лояльности"""
    user_id = callback.from_user.id

    async with get_db() as db:
        lm = LoyaltyManager(db)
        stats = await lm.get_user_stats(user_id)

    if not stats:
        await safe_answer_callback(callback, "Ошибка загрузки", show_alert=True)
        return

    text = (
        f"💎 <b>Программа лояльности</b>\n\n"
        f"Уровень: {stats['level_name']}\n"
        f"Кешбэк: {stats['cashback']}%\n\n"
        f"💰 Потрачено всего: {stats['total_spent']}₽\n"
        f"🎯 Баллов: {stats['points']}\n\n"
    )

    if stats["next_level"]:
        text += f"📈 До уровня {stats['next_level']}: {stats['next_threshold'] - stats['total_spent']}₽\n"
        text += f"Прогресс: {stats['progress']}%\n\n"

    text += f"🔗 Ваш код: <code>{stats['referral_code']}</code>\n"
    text += f"Пригласите друга и получите {REFERRAL_BONUS} баллов!"

    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Использовать промокод", callback_data="enter_promo")
    builder.button(text="🔙 Назад", callback_data="back_to_main")

    await safe_edit_text(
        callback,
        text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery):
    """Профиль пользователя"""
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT username, first_name, phone, email, birth_date, "
            "total_spent, loyalty_points, loyalty_level, referral_code "
            "FROM users WHERE user_id = ?",
            (user_id,),
        )
        user = await cursor.fetchone()

    if not user:
        await callback.answer("Ошибка загрузки профиля", show_alert=True)
        return

    (
        username,
        first_name,
        phone,
        email,
        birth_date,
        total_spent,
        points,
        level,
        ref_code,
    ) = user

    text = f"👤 <b>Профиль</b>\n\nИмя: {first_name}\n"
    if username:
        text += f"Username: @{username}\n"
    if phone:
        text += f"Телефон: {phone}\n"
    if email:
        text += f"Email: {email}\n"
    if birth_date:
        text += f"День рождения: {birth_date}\n"

    text += (
        f"\n💰 Потрачено: {total_spent}₽\n🎯 Баллов: {points}\n📊 Уровень: {level}\n"
    )
    text += f"🔗 Реферальный код: <code>{ref_code}</code>"

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Изменить данные", callback_data="edit_profile")
    builder.button(text="🔙 Назад", callback_data="back_to_main")

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "edit_profile")
async def edit_profile_handler(callback: CallbackQuery, state: FSMContext):
    """Редактирование профиля"""
    text = (
        "📝 <b>Редактирование профиля</b>\n\n"
        "Отправьте данные в формате:\n"
        "<i>Телефон | Email | Дата рождения (ДД.ММ.ГГГГ)</i>\n\n"
        "Или отправьте по одному полю:\n"
        "• Телефон: +79991234567\n"
        "• Email: example@mail.ru\n"
        "• Дата рождения: 01.01.1990\n\n"
        "Для пропуска поля напишите '-'"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="profile")

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.entering_profile_data)


@user_router.message(UserStates.entering_profile_data)
async def process_edit_profile(message: Message, state: FSMContext):
    """Обработка данных профиля"""
    user_id = message.from_user.id
    text = message.text.strip()

    async with get_db() as db:
        try:
            if "|" in text:
                parts = text.split("|")
                phone = (
                    parts[0].strip()
                    if len(parts) > 0 and parts[0].strip() != "-"
                    else None
                )
                email = (
                    parts[1].strip()
                    if len(parts) > 1 and parts[1].strip() != "-"
                    else None
                )
                birth_date = (
                    parts[2].strip()
                    if len(parts) > 2 and parts[2].strip() != "-"
                    else None
                )

                if phone and validate_phone(
                    phone.replace("+", "").replace(" ", "").replace("-", "")
                ):
                    await db.execute(
                        "UPDATE users SET phone = ? WHERE user_id = ?",
                        (format_phone(phone), user_id),
                    )
                if email and validate_email(email):
                    await db.execute(
                        "UPDATE users SET email = ? WHERE user_id = ?", (email, user_id)
                    )
                if birth_date and validate_birth_date(birth_date):
                    await db.execute(
                        "UPDATE users SET birth_date = ? WHERE user_id = ?",
                        (birth_date, user_id),
                    )
            else:
                cleaned = text.replace("+", "").replace(" ", "").replace("-", "")
                if validate_phone(cleaned):
                    await db.execute(
                        "UPDATE users SET phone = ? WHERE user_id = ?",
                        (format_phone(text), user_id),
                    )
                elif validate_email(text):
                    await db.execute(
                        "UPDATE users SET email = ? WHERE user_id = ?", (text, user_id)
                    )
                elif validate_birth_date(text):
                    await db.execute(
                        "UPDATE users SET birth_date = ? WHERE user_id = ?",
                        (text, user_id),
                    )
                else:
                    await message.answer(
                        "❌ Не удалось распознать данные. Используйте формат:\n"
                        "<i>Телефон | Email | Дата рождения</i>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

            await message.answer("✅ Данные обновены!")
        except Exception as e:
            logger.error(f"Ошибка обновления профиля: {e}", exc_info=True)
            await message.answer("❌ Произошла ошибка. Попробуйте позже.")
            return

    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Мой профиль", callback_data="profile")
    builder.adjust(1)
    await message.answer("🏠 Главное меню", reply_markup=builder.as_markup())


@user_router.callback_query(F.data == "cafe_info")
async def show_cafe_info(callback: CallbackQuery):
    """Информация о кафе"""
    from app.config import CAFE_ADDRESS, CAFE_PHONE, PAYMENT_CARD, CAFE_LAT, CAFE_LON

    text = (
        f"📍 <b>Информация о кафе</b>\n\n"
        f"📌 Адрес: {CAFE_ADDRESS}\n"
        f"📞 Телефон: {CAFE_PHONE}\n"
        f"🕐 Время работы: 10:00 - 23:00\n\n"
        f"💳 Оплата: Перевод на карту <code>{PAYMENT_CARD}</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🗺️ Показать на карте", callback_data="show_on_map")
    builder.button(text="🔙 Назад", callback_data="back_to_main")

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "show_on_map")
async def show_on_map(callback: CallbackQuery):
    """Показать на карте"""
    from app.config import CAFE_LAT, CAFE_LON

    await callback.message.answer_location(CAFE_LAT, CAFE_LON)
    await callback.answer()


@user_router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
    """Мои заказы"""
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, created_at, total_price, status FROM orders "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        )
        orders = await cursor.fetchall()

    if not orders:
        await callback.message.edit_text(
            "📦 <b>Мои заказы</b>\n\nУ вас пока нет заказов.",
            reply_markup=get_back_keyboard("main"),
            parse_mode=ParseMode.HTML,
        )
        return

    status_icons = {
        "new": "🆕",
        "accepted": "✅",
        "cooking": "👨‍🍳",
        "ready": "📦",
        "delivering": "🚚",
        "completed": "✔️",
        "cancelled": "❌",
    }

    text = "📦 <b>Мои заказы</b>\n\n"
    for order in orders:
        order_id, created_at, total, status = order
        date = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        icon = status_icons.get(status, "❓")
        text += f"{icon} Заказ #{order_id} — {total}₽\n   {date}\n\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="back_to_main")

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data == "favorites")
async def show_favorites(callback: CallbackQuery):
    """Избранное"""
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT favorite_dishes FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()

        if not row or not row[0]:
            await callback.answer("У вас пока нет избранных блюд", show_alert=True)
            return

        favorites = json.loads(row[0])
        if not favorites:
            await callback.answer("У вас пока нет избранных блюд", show_alert=True)
            return

        placeholders = ",".join("?" for _ in favorites)
        cursor = await db.execute(
            f"SELECT * FROM menu WHERE id IN ({placeholders})", favorites
        )
        dishes = await cursor.fetchall()

    text = "⭐ <b>Избранное</b>\n\n"
    builder = InlineKeyboardBuilder()

    for dish in dishes:
        text += f"• <b>{dish[1]}</b> — {dish[3]}₽\n"
        builder.button(
            text=f"🔄 Заказать {dish[1][:20]}", callback_data=f"quick_order_{dish[0]}"
        )

    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(1)

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )


@user_router.callback_query(F.data.startswith("quick_order_"))
async def quick_order(callback: CallbackQuery):
    """Быстрый повтор заказа"""
    dish_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM menu WHERE id = ?", (dish_id,))
        dish = await cursor.fetchone()

        if not dish:
            await callback.answer("Блюдо не найдено", show_alert=True)
            return

        await db.execute(
            "INSERT INTO cart (user_id, dish_id, dish_name, ingredients, extra_price, base_price, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, dish_id, dish[1], "", 0, dish[3], datetime.now().isoformat()),
        )

    await callback.answer("✅ Добавлено в корзину!", show_alert=False)
    await callback.message.edit_text(
        "🛒 <b>Корзина</b>\n",
        reply_markup=InlineKeyboardBuilder()
        .button(text="🍽️ В меню", callback_data="menu")
        .button(text="🔙 Назад", callback_data="back_to_main")
        .as_markup(),
        parse_mode=ParseMode.HTML,
    )


@user_router.callback_query(F.data == "dish_of_day")
async def show_dish_of_day(callback: CallbackQuery):
    """Блюдо дня"""
    async with get_db() as db:
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = await db.execute(
            "SELECT m.*, dod.special_price FROM dish_of_day dod "
            "JOIN menu m ON dod.dish_id = m.id WHERE dod.date = ?",
            (today,),
        )
        dish = await cursor.fetchone()

        if not dish:
            await callback.answer("Сегодня блюдо дня не установлено", show_alert=True)
            return

        text = f"🍲 <b>Блюдо дня!</b>\n\n<b>{dish[1]}</b>\n{dish[2]}\n\n"

        if dish[13]:
            text += f"💰 <s>{dish[3]}₽</s> → <b>{dish[13]}₽</b>\n"
        else:
            text += f"💰 {dish[3]}₽\n"

        text += f"⭐ {dish[12] if len(dish) > 12 else 5.0}/5\n⏱️ {dish[8] if len(dish) > 8 else '?'}мин"

        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Заказать", callback_data=f"dish_{dish[0]}")
        builder.button(text="🔙 Назад", callback_data="back_to_main")

        await callback.message.edit_text(
            text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
        )


@user_router.callback_query(F.data.startswith("dish_"))
async def show_dish_details(callback: CallbackQuery):
    """Детали блюда"""
    dish_id = int(callback.data.split("_")[1])

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM menu WHERE id = ?", (dish_id,))
        dish = await cursor.fetchone()

        if not dish:
            await callback.answer("Блюдо не найдено", show_alert=True)
            return

        text = f"<b>{dish[1]}</b>\n\n{dish[2]}\n\n"
        text += f"💰 Цена: {dish[3]}₽\n"
        text += f"⭐ Рейтинг: {dish[12] if len(dish) > 12 else 5.0}/5\n"
        text += f"⏱️ Время приготовления: {dish[8] if len(dish) > 8 else '?'}мин\n"

        if dish[9]:
            try:
                allergens = json.loads(dish[9])
                if allergens:
                    text += f"⚠️ Аллергены: {', '.join(allergens)}\n"
            except json.JSONDecodeError:
                pass

        if dish[10]:
            try:
                tags = json.loads(dish[10])
                if tags:
                    text += f"🏷️ Диеты: {', '.join(tags)}\n"
            except json.JSONDecodeError:
                pass

        builder = InlineKeyboardBuilder()
        builder.button(text="➕ В корзину", callback_data=f"add_to_cart_{dish_id}")
        builder.button(text="⭐ В избранное", callback_data=f"favorite_{dish_id}")
        builder.button(text="🔙 Назад", callback_data="menu")

        # Если есть фото, отправляем его
        if dish[5]:  # image_url
            await callback.message.delete()
            await callback.message.answer_photo(
                photo=dish[5],  # file_id
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML,
            )
        else:
            await callback.message.edit_text(
                text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
            )


@user_router.callback_query(F.data.startswith("add_to_cart_"))
async def add_to_cart(callback: CallbackQuery):
    """Добавить в корзину"""
    dish_id = int(callback.data.split("_")[3])
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT name, price FROM menu WHERE id = ?", (dish_id,)
        )
        dish = await cursor.fetchone()

        if not dish:
            await callback.answer("Блюдо не найдено", show_alert=True)
            return

        await db.execute(
            "INSERT INTO cart (user_id, dish_id, dish_name, ingredients, extra_price, base_price, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, dish_id, dish[0], "", 0, dish[1], datetime.now().isoformat()),
        )

    await callback.answer(f"✅ {dish[0]} добавлен в корзину!", show_alert=False)
    await _show_cart_ui(callback)


@user_router.callback_query(F.data.startswith("favorite_"))
async def toggle_favorite(callback: CallbackQuery):
    """Переключить избранное"""
    dish_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT favorite_dishes FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        favorites = json.loads(row[0]) if row and row[0] else []

        if dish_id in favorites:
            favorites.remove(dish_id)
            msg = "Удалено из избранного"
        else:
            favorites.append(dish_id)
            msg = "Добавлено в избранное"

        await db.execute(
            "UPDATE users SET favorite_dishes = ? WHERE user_id = ?",
            (json.dumps(favorites), user_id),
        )

    await callback.answer(msg, show_alert=False)


async def _get_cart_count(user_id: int) -> int:
    """Количество товаров в корзине"""
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM cart WHERE user_id = ?", (user_id,))
            result = await cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        logger.error(f"Ошибка при получении количества товаров в корзине для пользователя {user_id}: {e}")
        return 0


async def _show_cart_ui(callback: CallbackQuery):
    """Показать корзину"""
    from app.states import UserStates
    import json

    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, dish_id, dish_name, base_price, extra_price, ingredients FROM cart WHERE user_id = ?",
            (user_id,),
        )
        cart_items = await cursor.fetchall()

    if not cart_items:
        builder = InlineKeyboardBuilder()
        builder.button(text="🍽️ Перейти в меню", callback_data="menu")
        builder.button(text="🔙 Назад", callback_data="back_to_main")
        builder.adjust(1)

        await callback.message.edit_text(
            "🛒 <b>Корзина пуста</b>\n\nДобавьте что-нибудь вкусное!",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML,
        )
        return

    subtotal = 0
    text = "🛒 <b>Ваш заказ</b>\n\n"

    for item in cart_items:
        item_id, dish_id, dish_name, base_price, extra_price, ingredients_json = item
        item_total = base_price + extra_price
        subtotal += item_total

        text += f"• <b>{dish_name}</b> — {base_price}₽"
        if extra_price > 0:
            text += f" (+{extra_price}₽ доп.)"

        # Отображение состава для кастомных блюд
        if dish_id == -1 and ingredients_json:
            try:
                ing_data = json.loads(ingredients_json)
                ings = ing_data.get("ingredients", [])
                sauces = ing_data.get("sauces", [])
                if ings:
                    text += "\n  🥘 " + ", ".join(i["name"] for i in ings)
                if sauces:
                    text += "\n  🫗 " + ", ".join(s["name"] for s in sauces)
            except (json.JSONDecodeError, TypeError):
                pass

        text += f"\n  Итого: {item_total}₽\n\n"

    delivery_cost = 199
    free_delivery_from = 1500
    if subtotal >= free_delivery_from:
        delivery_cost = 0
        text += f"🚚 Доставка: <b>бесплатно</b> (от {free_delivery_from}₽)\n\n"
    else:
        text += f"🚚 Доставка: {delivery_cost}₽\n"
        text += f"(Бесплатно от {free_delivery_from}₽)\n\n"

    total = subtotal + delivery_cost

    text += f"💰 <b>Итого: {total}₽</b>\n"
    text += f"   Товары: {subtotal}₽\n"
    text += f"   Доставка: {delivery_cost}₽\n"

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT loyalty_points FROM users WHERE user_id = ?", (user_id,)
        )
        points_row = await cursor.fetchone()
        points = points_row[0] if points_row else 0

    builder = InlineKeyboardBuilder()

    if points > 0:
        max_points_use = min(points, int(subtotal * 0.3))
        if max_points_use > 0:
            builder.button(
                text=f"🎯 Использовать баллы (до {max_points_use})",
                callback_data=f"use_points_{max_points_use}",
            )

    builder.button(text="🎁 Промокод", callback_data="enter_promo_order")
    builder.button(text="📝 Оформить заказ", callback_data="checkout")
    builder.button(text="🗑️ Очистить корзину", callback_data="clear_cart")
    builder.button(text="🔙 Назад", callback_data="back_to_main")
    builder.adjust(1)

    await callback.message.edit_text(
        text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML
    )
