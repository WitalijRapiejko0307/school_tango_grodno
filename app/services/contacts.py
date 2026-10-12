"""Contact card application services."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContactCard


async def update_contact(
    session: AsyncSession,
    contact_id: str,
    name: str,
    phone: str,
    role_label: str,
) -> ContactCard:
    name = name.strip()
    phone = phone.strip()
    role_label = role_label.strip()
    if not name or not phone or not role_label:
        raise ValueError("name, phone, and role_label are required")

    card = await session.get(ContactCard, contact_id)
    if card is None:
        raise ValueError("contact")

    card.name = name
    card.phone = phone
    card.role_label = role_label
    await session.flush()
    return card


async def archive_contact(
    session: AsyncSession, contact_id: str, now: datetime
) -> ContactCard:
    card = await session.get(ContactCard, contact_id)
    if card is None:
        raise ValueError("contact")
    card.archived_at = now
    await session.flush()
    return card


async def restore_contact(session: AsyncSession, contact_id: str) -> ContactCard:
    card = await session.get(ContactCard, contact_id)
    if card is None:
        raise ValueError("contact")
    card.archived_at = None
    await session.flush()
    return card


async def list_contacts(session: AsyncSession, archived: bool) -> list[ContactCard]:
    query = select(ContactCard).order_by(ContactCard.name)
    if archived:
        query = query.where(ContactCard.archived_at.is_not(None))
    else:
        query = query.where(ContactCard.archived_at.is_(None))
    result = await session.execute(query)
    return list(result.scalars().all())
