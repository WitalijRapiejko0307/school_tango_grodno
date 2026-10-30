"""Permanent deletion of archived entities.

Dependent rows (subscriptions, payments, sessions, attendance, ...) are kept. They
hold the old id without a foreign key, and `deleted_entities` keeps the name and
type the entity had when it was deleted.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ContactCard,
    DeletedEntity,
    Group,
    GroupMembership,
    Person,
    Product,
    SchoolSession,
    WeeklySlot,
)

PURGE_KINDS = ("person", "group", "contact", "product", "slot")

_TYPE_LABELS = {
    "person": "клиент",
    "group": "группа",
    "contact": "контакт",
    "product": "продукт",
    "slot": "слот",
}


class NotArchivedError(ValueError):
    """The entity is missing or still active."""


async def _archived_entity(session: AsyncSession, kind: str, entity_id: str):
    """Return (row, label) for an archived entity, else raise NotArchivedError."""
    if kind == "person":
        row = await session.get(Person, entity_id)
        ok = row is not None and row.archived_at is not None and row.role != "admin"
        label = row.full_name if row is not None else ""
    elif kind == "group":
        row = await session.get(Group, entity_id)
        ok = row is not None and row.archived_at is not None
        label = row.name if row is not None else ""
    elif kind == "contact":
        row = await session.get(ContactCard, entity_id)
        ok = row is not None and row.archived_at is not None
        label = row.name if row is not None else ""
    elif kind == "product":
        row = await session.get(Product, entity_id)
        ok = row is not None and not row.active
        label = row.name if row is not None else ""
    elif kind == "slot":
        row = await session.get(WeeklySlot, entity_id)
        ok = row is not None and not row.active
        label = ""
        if row is not None:
            group = await session.get(Group, row.group_id)
            gname = group.name if group is not None else "?"
            label = f"{gname} {row.weekday + 1} {row.start_time:%H:%M}"
    else:
        raise NotArchivedError(kind)
    if not ok:
        raise NotArchivedError(kind)
    return row, label


async def purge_preview(session: AsyncSession, kind: str, entity_id: str) -> str:
    """Text for the confirmation dialog."""
    _row, label = await _archived_entity(session, kind, entity_id)
    text = f"Удалить навсегда: {label}?"
    if kind == "group":
        members = (
            await session.execute(
                select(func.count())
                .select_from(GroupMembership)
                .where(GroupMembership.group_id == entity_id)
            )
        ).scalar_one()
        sessions = (
            await session.execute(
                select(func.count())
                .select_from(SchoolSession)
                .where(SchoolSession.group_id == entity_id)
            )
        ).scalar_one()
        text += (
            f"\nУчастников в записях: {members}, занятий: {sessions}. "
            "Они останутся с пометкой «группа удалена»."
        )
    else:
        text += "\nСвязанные записи останутся с пометкой об удалении."
    return text


async def purge_entity(session: AsyncSession, kind: str, entity_id: str) -> str:
    """Delete one archived entity; return its label."""
    row, label = await _archived_entity(session, kind, entity_id)
    existing = await session.execute(
        select(DeletedEntity).where(
            DeletedEntity.entity_type == kind, DeletedEntity.entity_id == entity_id
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(
            DeletedEntity(entity_type=kind, entity_id=entity_id, label=label)
        )
    await session.delete(row)
    await session.flush()
    return label


async def deleted_label(
    session: AsyncSession, kind: str, entity_id: str | None
) -> str:
    """Display text for a reference whose target row is gone."""
    if entity_id is None:
        return "?"
    result = await session.execute(
        select(DeletedEntity.label).where(
            DeletedEntity.entity_type == kind, DeletedEntity.entity_id == entity_id
        )
    )
    label = result.scalar_one_or_none()
    type_label = _TYPE_LABELS.get(kind, kind)
    if label is None:
        return f"? ({type_label} удалён)"
    return f"{label} ({type_label} удалён)"
