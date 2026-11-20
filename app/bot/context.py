"""Handlerlarga uzatiladigan umumiy servislar to'plami (dependency injection)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.orchestrator import Orchestrator
from app.core.security import RateLimiter, UserLocks
from app.services.ai_service import AIService
from app.services.channel_service import ChannelService
from app.services.payment_service import PaymentService
from app.services.runtime_settings import RuntimeSettings
from app.services.subscription_service import SubscriptionService


@dataclass
class AppContext:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    ai: AIService
    orchestrator: Orchestrator
    payments: PaymentService
    channel: ChannelService
    rate_limiter: RateLimiter
    user_locks: UserLocks
    runtime: RuntimeSettings
    subscriptions: SubscriptionService
    bot_username: str = ""
