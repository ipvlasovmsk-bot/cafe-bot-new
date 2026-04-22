"""Обработчики корзины и оформления заказа"""
import logging
import json
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database import get_db
from app.states import UserStates
from app.services.loyalty import LoyaltySystem, LoyaltyManager
from app.keyboards.main import get_back_keyboard
from app.utils.validators import validate_time_format, validate_address

logger = logging.getLogger(__name__)
cart_router = Router()


@cart_router.callback_query(F.data == "cart")
async def show_cart_handler(callback: CallbackQuery):
    """Показать корзину"""
    from app.handlers.user_handlers import _show_cart_ui, _get_cart_count
    await _show_cart_ui(callback)


@cart_router.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: CallbackQuery):
    """Очистить корзину"""
    user_id = callback.from_user.id

    async with get_db() as db:
        await db.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))

    await callback.answer("Корзина очищена", show_alert=False)
    from app.handlers.user_handlers import _show_cart_ui, _get_cart_count
    await _show_cart_ui(callback)


@cart_router.callback_query(F.data == "checkout")
async def checkout_handler(callback: CallbackQuery, state: FSMContext):
    """Начало оформления заказа"""
    user_id = callback.from_user.id

    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM cart WHERE user_id = ?", (user_id,))
        cart_count = (await cursor.fetchone())[0]

    if cart_count == 0:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    text = (
        "📍 <b>Адрес доставки</b>\n\n"
        "Введите адрес доставки в формате:\n"
        "<i>ул. Примерная, д. 1, кв. 10</i>\n\n"
        "Или отправьте геолокацию 📍"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к корзине", callback_data="cart")

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    await state.set_state(UserStates.entering_address)


@cart_router.message(UserStates.entering_address)
async def process_address(message: Message, state: FSMContext):
    """Обработка адреса"""
    address = message.text.strip()

    if not validate_address(address):
        await message.answer(
            "❌ Адрес слишком короткий. Введите полный адрес, например:\n"
            "<i>ул. Примерная, д. 1, кв. 10</i>",
            parse_mode=ParseMode.HTML
        )
        return

    await state.update_data(address=address)

    text = (
        "🕐 <b>Время доставки</b>\n\n"
        "Выберите когда доставить:"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Как можно скорее", callback_data="delivery_asap")
    builder.button(text="📅 К определенному времени", callback_data="time_custom")
    builder.button(text="🔙 Назад", callback_data="cart")
    builder.adjust(1)

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    await state.set_state(UserStates.selecting_delivery_time)


@cart_router.callback_query(F.data == "delivery_asap")
async def delivery_asap(callback: CallbackQuery, state: FSMContext):
    """Доставка как можно скорее"""
    delivery_time = (datetime.now() + timedelta(minutes=45)).isoformat()
    await state.update_data(delivery_time=delivery_time)
    await _show_order_summary(callback, state)


@cart_router.callback_query(F.data == "time_custom")
async def time_custom(callback: CallbackQuery, state: FSMContext):
    """Выбор времени"""
    text = (
        "📅 <b>Выберите дату и время</b>\n\n"
        "Напишите желаемую дату и время в формате:\n"
        "<i>ДД.ММ ЧЧ:ММ</i>\n\n"
        "Например: 15.04 14:00"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="cart")

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)


@cart_router.message(UserStates.selecting_delivery_time)
async def process_delivery_time(message: Message, state: FSMContext):
    """Обработка времени доставки"""
    parsed_time = validate_time_format(message.text)

    if not parsed_time:
        await message.answer(
            "❌ Неверный формат. Используйте: <i>ДД.ММ ЧЧ:ММ</i>\n"
            "Например: 15.04 14:00",
            parse_mode=ParseMode.HTML
        )
        return

    if parsed_time < datetime.now():
        await message.answer("❌ Время должно быть в будущем")
        return

    await state.update_data(delivery_time=parsed_time.isoformat())
    await _show_order_summary(message, state, is_message=True)


@cart_router.callback_query(F.data.startswith("use_points_"))
async def use_points_handler(callback: CallbackQuery, state: FSMContext):
    """Использовать баллы"""
    points_to_use = int(callback.data.split("_")[2])
    await state.update_data(points_used=points_to_use)
    await callback.answer(f"Использовано {points_to_use} баллов", show_alert=False)
    await _show_order_summary(callback, state)


@cart_router.callback_query(F.data == "enter_promo")
@cart_router.callback_query(F.data == "enter_promo_order")
async def enter_promo_handler(callback: CallbackQuery, state: FSMContext):
    """Ввод промокода"""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="cart")

    await callback.message.edit_text(
        "🎁 <b>Введите промокод:</b>",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.entering_promo_code)


@cart_router.message(UserStates.entering_promo_code)
async def process_promo(message: Message, state: FSMContext):
    """Обработка промокода"""
    promo_code = message.text.strip().upper()

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT type, value, min_order, max_uses, used_count, valid_until "
            "FROM promo_codes WHERE code = ?",
            (promo_code,)
        )
        promo = await cursor.fetchone()

        if not promo:
            await message.answer("❌ Промокод не найден")
            await state.clear()
            return

        promo_type, value, min_order, max_uses, used_count, valid_until = promo

        if valid_until:
            try:
                if datetime.now() > datetime.fromisoformat(valid_until):
                    await message.answer("❌ Срок действия промокода истёк")
                    await state.clear()
                    return
            except ValueError:
                pass

        if max_uses and used_count >= max_uses:
            await message.answer("❌ Промокод уже использован")
            await state.clear()
            return

        promo_desc = {
            'percent': f"Скидка {value}%",
            'fixed': f"Скидка {value}₽",
            'free_delivery': "Бесплатная доставка"
        }.get(promo_type, "Скидка")

        await state.update_data(promo_code=promo_code, promo_type=promo_type, promo_value=value)
        await message.answer(f"✅ Промокод применён: {promo_desc}")

        # Переходим к сводке
        from app.handlers.user_handlers import _show_cart_ui, _get_cart_count
        await _show_cart_ui(message)
        await state.clear()


@cart_router.callback_query(F.data == "pay_order")
async def pay_order_handler(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Оплата через QR-код"""
    from app.config import PAYMENT_QR_PATH
    import os
    from pathlib import Path
    
    data = await state.get_data()
    total = data.get('total', 0)

    text = (
        f"💳 <b>Оплата заказа на {total}₽</b>\n\n"
        f"📱 <b>Отсканируйте QR-код для оплаты</b>\n\n"
        f"После оплаты нажмите 'Подтвердить'."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить оплату", callback_data="confirm_payment")
    builder.button(text="❌ Отмена", callback_data="cancel_order")
    builder.adjust(1)

    # Проверяем существование файла с QR-кодом
    qr_path = Path(PAYMENT_QR_PATH)
    
    try:
        # Отправляем фото QR-кода если файл существует
        if qr_path.exists():
            await callback.message.answer_photo(
                photo=BufferedInputFile.from_file(qr_path),
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML
            )
        else:
            # Если файла нет, показываем предупреждение
            await callback.message.edit_text(
                "⚠️ <b>QR-код для оплаты не настроен</b>\n\n"
                "Пожалуйста, обратитесь к администратору для совершения оплаты.",
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML
            )
            logger.warning(f"QR-код не найден: {PAYMENT_QR_PATH}")
    
    except Exception as e:
        logger.error(f"Ошибка отправки QR-кода: {e}")
        # Fallback - показываем сообщение об ошибке
        await callback.message.edit_text(
            "⚠️ <b>Ошибка отображения QR-кода</b>\n\n"
            "Пожалуйста, обратитесь к администратору для совершения оплаты.",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )


@cart_router.callback_query(F.data == "confirm_payment")
async def confirm_payment_handler(callback: CallbackQuery, state: FSMContext):
    """Подтверждение оплаты"""
    user_id = callback.from_user.id
    data = await state.get_data()

    async with get_db() as db:
        try:
            cursor = await db.execute(
                "SELECT dish_id, dish_name, base_price, extra_price, ingredients FROM cart WHERE user_id = ?",
                (user_id,)
            )
            items = await cursor.fetchall()

            if not items:
                await callback.answer("Корзина пуста!", show_alert=True)
                return

            items_list = []
            for i in items:
                item_dict = {
                    'dish_id': i[0],
                    'dish_name': i[1],
                    'base_price': i[2],
                    'extra_price': i[3],
                }
                if i[4]:  # ingredients JSON
                    try:
                        item_dict['ingredients'] = json.loads(i[4])
                    except (json.JSONDecodeError, TypeError):
                        item_dict['ingredients'] = i[4]
                items_list.append(item_dict)

            items_json = json.dumps(items_list, ensure_ascii=False)

            total = data.get('total', 0)
            subtotal = data.get('subtotal', 0)
            points_used = data.get('points_used', 0)
            promo_discount = data.get('promo_discount', 0)

            # Кешбэк
            lm = LoyaltyManager(db)
            user_stats = await lm.get_user_stats(user_id)
            cashback_percent = user_stats['cashback'] if user_stats else 0
            cashback = int((subtotal - promo_discount) * cashback_percent / 100)

            cursor = await db.execute(
                "INSERT INTO orders "
                "(user_id, items, total_price, status, points_earned, points_spent, "
                "discount_applied, promo_code, delivery_time, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, items_json, total, 'new', cashback, points_used,
                 promo_discount, data.get('promo_code'), data.get('delivery_time'),
                 datetime.now().isoformat(), datetime.now().isoformat())
            )
            order_id = cursor.lastrowid

            if points_used > 0:
                await lm.spend_points(user_id, points_used, order_id)

            if cashback > 0:
                await lm.add_points(user_id, cashback, order_id)

            await db.execute(
                "UPDATE users SET total_spent = total_spent + ? WHERE user_id = ?",
                (subtotal - promo_discount, user_id)
            )

            cursor = await db.execute("SELECT total_spent FROM users WHERE user_id = ?", (user_id,))
            total_spent = (await cursor.fetchone())[0]
            new_level = LoyaltySystem.calculate_level(total_spent).value
            await db.execute("UPDATE users SET loyalty_level = ? WHERE user_id = ?", (new_level, user_id))

            await db.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))

            logger.info(f"✅ Заказ #{order_id} создан пользователем {user_id}, сумма: {total}₽")

        except Exception as e:
            logger.error(f"Ошибка создания заказа: {e}", exc_info=True)
            await callback.answer(
                "Произошла ошибка при создании заказа. Попробуйте позже.",
                show_alert=True
            )
            return

    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.button(text="🍽️ В меню", callback_data="menu")
    builder.button(text="📦 Мои заказы", callback_data="my_orders")
    builder.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Заказ принят!</b>\n\n"
        f"Номер заказа: #{order_id}\n"
        f"Сумма: {total}₽\n"
        f"Начислено баллов: {cashback}\n\n"
        f"Мы сообщим вам о готовности заказа!",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )


@cart_router.callback_query(F.data == "cancel_order")
async def cancel_order_handler(callback: CallbackQuery, state: FSMContext):
    """Отмена заказа"""
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В главное меню", callback_data="back_to_main")
    await callback.message.edit_text(
        "❌ Заказ отменён",
        reply_markup=builder.as_markup()
    )


async def _show_order_summary(event, state: FSMContext, is_message: bool = False):
    """Сводка заказа"""
    data = await state.get_data()
    subtotal = data.get('subtotal', 0)
    delivery_cost = data.get('delivery_cost', 199)
    promo_discount = data.get('promo_discount', 0)
    points_used = data.get('points_used', 0)

    total = max(0, subtotal + delivery_cost - promo_discount - points_used)
    await state.update_data(total=total)

    text = "📋 <b>Сводка заказа</b>\n\n"
    text += f"🛒 Товары: {subtotal}₽\n"
    text += f"🚚 Доставка: {delivery_cost if delivery_cost > 0 else 0}₽\n"

    if promo_discount > 0:
        text += f"🎁 Скидка по промокоду: -{promo_discount}₽\n"
    if points_used > 0:
        text += f"🎯 Списано баллов: -{points_used}\n"

    text += f"\n💰 <b>Итого к оплате: {total}₽</b>"

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить", callback_data="pay_order")
    builder.button(text="🔙 Назад к корзине", callback_data="cart")
    builder.button(text="❌ Отмена", callback_data="cancel_order")
    builder.adjust(1)

    if is_message and hasattr(event, 'answer'):
        await event.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    elif hasattr(event, 'message') and hasattr(event.message, 'edit_text'):
        await event.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
