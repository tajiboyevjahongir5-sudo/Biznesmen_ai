"""Database bilan ishlash funksiyalari.

Barcha so'rovlar SQLAlchemy orqali parametrli bajariladi (SQL injection yo'q).
Loyiha bilan bog'liq har bir funksiya `user_id` ni tekshiradi — foydalanuvchi
faqat o'z loyihalariga kira oladi.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AgentAnalysis,
    BusinessProject,
    ChannelIdea,
    Conversation,
    IdeaStatus,
    ProjectStatus,
    PromptVersion,
    ResearchNote,
    User,
)

ACTIVE_STATUSES = (ProjectStatus.INTERVIEWING, ProjectStatus.ANALYZING)


# --------------------------------------------------------------------- users


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None, first_name: str | None
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name)
        session.add(user)
        await session.flush()
    else:
        user.username = username
        user.first_name = first_name
    return user


async def get_user_by_tg(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


# ------------------------------------------------------------------ projects


async def get_active_project(session: AsyncSession, user_id: int) -> BusinessProject | None:
    return await session.scalar(
        select(BusinessProject)
        .where(BusinessProject.user_id == user_id, BusinessProject.status.in_(ACTIVE_STATUSES))
        .order_by(BusinessProject.id.desc())
        .limit(1)
    )


async def create_project(session: AsyncSession, user_id: int, idea: str) -> BusinessProject:
    # Bitta foydalanuvchida bitta faol suhbat: eskisini bekor qilamiz.
    for old in (
        await session.scalars(
            select(BusinessProject).where(
                BusinessProject.user_id == user_id, BusinessProject.status.in_(ACTIVE_STATUSES)
            )
        )
    ).all():
        old.status = ProjectStatus.CANCELLED
    project = BusinessProject(user_id=user_id, idea=idea, title=idea[:80])
    session.add(project)
    await session.flush()
    return project


async def get_project(session: AsyncSession, user_id: int, project_id: int) -> BusinessProject | None:
    """Loyihani faqat egasi uchun qaytaradi."""
    return await session.scalar(
        select(BusinessProject).where(BusinessProject.id == project_id, BusinessProject.user_id == user_id)
    )


async def list_projects(session: AsyncSession, user_id: int, limit: int = 10) -> list[BusinessProject]:
    rows = await session.scalars(
        select(BusinessProject)
        .where(
            BusinessProject.user_id == user_id,
            BusinessProject.status != ProjectStatus.CANCELLED,
        )
        .order_by(BusinessProject.id.desc())
        .limit(limit)
    )
    return list(rows.all())


async def delete_project(session: AsyncSession, user_id: int, project_id: int) -> bool:
    """Loyihani va unga tegishli barcha yozuvlarni butunlay o'chiradi."""
    project = await get_project(session, user_id, project_id)
    if project is None:
        return False
    for model in (Conversation, AgentAnalysis, PromptVersion):
        await session.execute(delete(model).where(model.project_id == project_id))
    await session.delete(project)
    return True


async def add_message(session: AsyncSession, project_id: int, role: str, message: str) -> None:
    session.add(Conversation(project_id=project_id, role=role, message=message))


async def get_messages(session: AsyncSession, project_id: int) -> list[Conversation]:
    rows = await session.scalars(
        select(Conversation).where(Conversation.project_id == project_id).order_by(Conversation.id)
    )
    return list(rows.all())


async def save_analysis(session: AsyncSession, project_id: int, agent: str, payload: str) -> None:
    session.add(AgentAnalysis(project_id=project_id, agent_name=agent, analysis=payload))


async def save_prompt(session: AsyncSession, project_id: int, prompt_text: str) -> PromptVersion:
    current = await session.scalar(
        select(func.max(PromptVersion.version)).where(PromptVersion.project_id == project_id)
    )
    version = PromptVersion(project_id=project_id, prompt_text=prompt_text, version=(current or 0) + 1)
    session.add(version)
    return version


async def latest_prompt(session: AsyncSession, user_id: int, project_id: int) -> str | None:
    project = await get_project(session, user_id, project_id)
    if project is None:
        return None
    return await session.scalar(
        select(PromptVersion.prompt_text)
        .where(PromptVersion.project_id == project_id)
        .order_by(PromptVersion.version.desc())
        .limit(1)
    )


# ------------------------------------------------------------- channel ideas


async def recent_idea_titles(session: AsyncSession, limit: int = 60) -> list[str]:
    rows = await session.scalars(select(ChannelIdea.title).order_by(ChannelIdea.id.desc()).limit(limit))
    return list(rows.all())


async def get_idea(session: AsyncSession, idea_id: int) -> ChannelIdea | None:
    return await session.get(ChannelIdea, idea_id)


async def list_ideas(session: AsyncSession, status: str | None = None, limit: int = 10) -> list[ChannelIdea]:
    stmt = select(ChannelIdea).order_by(ChannelIdea.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(ChannelIdea.status == status)
    return list((await session.scalars(stmt)).all())


async def count_ideas(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(ChannelIdea.status, func.count()).group_by(ChannelIdea.status))
    result = {IdeaStatus.DRAFT: 0, IdeaStatus.PUBLISHED: 0, IdeaStatus.REJECTED: 0}
    result.update({status: n for status, n in rows.all()})
    return result


# ----------------------------------------------------------- research notes


async def add_research_note(
    session: AsyncSession, topic: str, content: str, sources_json: str, web: bool
) -> None:
    session.add(ResearchNote(topic=topic[:200], content=content, sources=sources_json, web_researched=web))


async def recent_research_notes(session: AsyncSession, limit: int = 5) -> list[ResearchNote]:
    rows = await session.scalars(select(ResearchNote).order_by(ResearchNote.id.desc()).limit(limit))
    return list(rows.all())


async def count_users(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User)) or 0)
