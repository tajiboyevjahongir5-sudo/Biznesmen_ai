"""SQLAlchemy 2 ORM modellari."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    promo_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    projects: Mapped[list[BusinessProject]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ProjectStatus:
    INTERVIEWING = "interviewing"
    ANALYZING = "analyzing"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"
    FOLLOWUP = "followup"  # tahlildan keyin natijalar bo'yicha kuzatuv suhbati


class BusinessProject(Base):
    __tablename__ = "business_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    idea: Mapped[str] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(120))
    location: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.INTERVIEWING, index=True)
    pending_question: Mapped[str | None] = mapped_column(Text)
    questions_asked: Mapped[int] = mapped_column(Integer, default=0)
    followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    followup_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="projects")
    messages: Mapped[list[Conversation]] = relationship(
        cascade="all, delete-orphan", order_by="Conversation.id"
    )
    analyses: Mapped[list[AgentAnalysis]] = relationship(cascade="all, delete-orphan")
    prompts: Mapped[list[PromptVersion]] = relationship(cascade="all, delete-orphan")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("business_projects.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # "assistant" | "user"
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentAnalysis(Base):
    __tablename__ = "agent_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("business_projects.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(40))
    analysis: Mapped[str] = mapped_column(Text)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromptVersion(Base):
    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("business_projects.id", ondelete="CASCADE"), index=True
    )
    prompt_text: Mapped[str] = mapped_column(Text)
    # Dasturiy mahsulotlar uchun texnik prompt (kod, xavfsizlik, UI/UX). Boshqalarda bo'sh.
    tech_prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdeaStatus:
    DRAFT = "draft"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ChannelIdea(Base):
    """Kanal uchun kunlik izlanishda topilgan biznes g'oya."""

    __tablename__ = "channel_ideas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    post_text: Mapped[str] = mapped_column(Text)  # kanalga chiqadigan qisqa matn (HTML)
    details: Mapped[str] = mapped_column(Text)  # JSON: to'liq tahlil
    prompt_text: Mapped[str] = mapped_column(Text)  # tugma bosilganda beriladigan prompt
    sources: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    web_researched: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=IdeaStatus.DRAFT, index=True)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    prompt_requests: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchNote(Base):
    """Kunlik izlanishdan qolgan bilim: keyingi tahlillarda kontekst sifatida ishlatiladi."""

    __tablename__ = "research_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[str] = mapped_column(Text, default="[]")
    web_researched: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentStatus:
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentReservation(Base):
    """Foydalanuvchiga berilgan noyob summa (narx + 1..99)."""

    __tablename__ = "payment_reservations"
    __table_args__ = (
        # Bir vaqtning o'zida bitta summa faqat bitta kutilayotgan to'lovga tegishli.
        Index(
            "uq_pending_amount",
            "amount",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    base_amount: Mapped[int] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PENDING, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    invite_link: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(40))  # "auto" | "admin:<id>"


class AppSetting(Base):
    """Admin panel orqali kiritilgan sozlamalar."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PaymentNotification(Base):
    """To'lov kanaliga kelgan bank xabari (audit uchun)."""

    __tablename__ = "payment_notifications"
    __table_args__ = (Index("uq_notif_msg", "chat_id", "message_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[int | None] = mapped_column(Integer)
    matched_reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_reservations.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16))  # matched | unmatched | ignored
    raw_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubscriptionStatus:
    ACTIVE = "active"
    EXPIRED = "expired"


class Subscription(Base):
    """Pullik kanalga kirish muddati (expires_at=None — muddatsiz)."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(16), default=SubscriptionStatus.ACTIVE, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    last_reservation_id: Mapped[int | None] = mapped_column(Integer)


class PendingJob(Base):
    """AI band bo'lganda navbatga olingan foydalanuvchi xabari (bot qayta ishga tushsa ham saqlanadi)."""

    __tablename__ = "pending_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    user_name: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(16), default="turn")
    project_id: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
