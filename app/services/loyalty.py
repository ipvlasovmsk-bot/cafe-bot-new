"""Система лояльности"""
import logging
import hashlib
from datetime import datetime
from typing import Optional

import aiosqlite
from aiogram import Bot

from app.models import LoyaltyLevel
from app.config import (
    LOYALTY_BRONZE_THRESHOLD, LOYALTY_SILVER_THRESHOLD, LOYALTY_GOLD_THRESHOLD,
    LOYALTY_CASHBACK_BRONZE, LOYALTY_CASHBACK_SILVER, LOYALTY_CASHBACK_GOLD,
    REFERRAL_BONUS
)

logger = logging.getLogger(__name__)


class LoyaltySystem:
    """Статические методы для расчёта лояльности"""

    @staticmethod
    def calculate_level(total_spent: int) -> LoyaltyLevel:
        if total_spent >= LOYALTY_GOLD_THRESHOLD:
            return LoyaltyLevel.GOLD
        elif total_spent >= LOYALTY_SILVER_THRESHOLD:
            return LoyaltyLevel.SILVER
        elif total_spent >= LOYALTY_BRONZE_THRESHOLD:
            return LoyaltyLevel.BRONZE
        return LoyaltyLevel.NONE

    @staticmethod
    def get_cashback_percent(level: LoyaltyLevel) -> float:
        return {
            LoyaltyLevel.NONE: 0,
            LoyaltyLevel.BRONZE: LOYALTY_CASHBACK_BRONZE,
            LoyaltyLevel.SILVER: LOYALTY_CASHBACK_SILVER,
            LoyaltyLevel.GOLD: LOYALTY_CASHBACK_GOLD
        }.get(level, 0)

    @staticmethod
    def generate_referral_code(user_id: int) -> str:
        hash_val = hashlib.md5(str(user_id).encode()).hexdigest()[:4].upper()
        return f"CAFE{user_id}{hash_val}"


class LoyaltyManager:
    """Управление лояльностью пользователя"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_user_stats(self, user_id: int) -> Optional[dict]:
        """Получить статистику лояльности пользователя"""
        cursor = await self.db.execute(
            "SELECT total_spent, loyalty_points, loyalty_level, referral_code, referred_by "
            "FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None

        level = LoyaltyLevel(row[2])
        next_level = None
        next_threshold = None

        if level == LoyaltyLevel.NONE:
            next_level = "BRONZE"
            next_threshold = LOYALTY_BRONZE_THRESHOLD
        elif level == LoyaltyLevel.BRONZE:
            next_level = "SILVER"
            next_threshold = LOYALTY_SILVER_THRESHOLD
        elif level == LoyaltyLevel.SILVER:
            next_level = "GOLD"
            next_threshold = LOYALTY_GOLD_THRESHOLD

        progress = 0
        if next_threshold:
            progress = min(100, int((row[0] / next_threshold) * 100))

        return {
            "total_spent": row[0],
            "points": row[1],
            "level": level.value,
            "level_name": self._get_level_name(level),
            "cashback": int(LoyaltySystem.get_cashback_percent(level) * 100),
            "next_level": next_level,
            "next_threshold": next_threshold,
            "progress": progress,
            "referral_code": row[3],
            "referred_by": row[4]
        }

    @staticmethod
    def _get_level_name(level: LoyaltyLevel) -> str:
        return {
            LoyaltyLevel.NONE: "🆕 Новичок",
            LoyaltyLevel.BRONZE: "🥉 Бронза",
            LoyaltyLevel.SILVER: "🥈 Серебро",
            LoyaltyLevel.GOLD: "🥇 Золото"
        }.get(level, "Неизвестно")

    async def add_points(self, user_id: int, amount: int, order_id: int | None = None):
        """Начислить баллы"""
        await self.db.execute(
            "UPDATE users SET loyalty_points = loyalty_points + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await self.db.execute(
            "INSERT INTO points_history (user_id, amount, type, order_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, "earn", order_id, datetime.now().isoformat())
        )

    async def spend_points(self, user_id: int, amount: int, order_id: int) -> bool:
        """Списать баллы"""
        cursor = await self.db.execute(
            "SELECT loyalty_points FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row or row[0] < amount:
            return False

        await self.db.execute(
            "UPDATE users SET loyalty_points = loyalty_points - ? WHERE user_id = ?",
            (amount, user_id)
        )
        await self.db.execute(
            "INSERT INTO points_history (user_id, amount, type, order_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, -amount, "spend", order_id, datetime.now().isoformat())
        )
        return True

    async def process_referral(self, new_user_id: int, referral_code: str) -> bool:
        """Обработка реферального кода"""
        cursor = await self.db.execute(
            "SELECT user_id FROM users WHERE referral_code = ?", (referral_code,)
        )
        referrer = await cursor.fetchone()

        if not referrer or referrer[0] == new_user_id:
            return False

        await self.add_points(referrer[0], REFERRAL_BONUS)
        await self.db.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (referrer[0], new_user_id)
        )

        logger.info(f"✅ Реферал: {referrer[0]} -> {new_user_id}, +{REFERRAL_BONUS} баллов")
        return True

    async def check_birthday(self, user_id: int) -> Optional[int]:
        """Проверка дня рождения — возвращает % скидки или None"""
        cursor = await self.db.execute(
            "SELECT birth_date FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None

        try:
            birth = datetime.strptime(row[0], "%d.%m.%Y")
            today = datetime.now()
            if birth.day == today.day and birth.month == today.month:
                return 15  # 15% скидка
        except ValueError:
            logger.warning(f"Некорректная дата рождения у пользователя {user_id}: {row[0]}")

        return None
