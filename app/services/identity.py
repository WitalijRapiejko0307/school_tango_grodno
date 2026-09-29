"""Identity and admin-invite application services."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AdminInvite, Person


def _normalize_username(username: str | None) -> str | None:
    if username is None:
        return None
    return username.lstrip("@").strip().lower() or None


def normalize_phone(phone: str | None) -> str | None:
    """Digits only, so +375… and 375… match the same invite."""
    if phone is None:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits or None


async def upsert_from_telegram(
    session: AsyncSession,
    telegram_user_id: int,
    username: str | None,
    full_name: str,
    phone: str | None = None,
) -> Person:
    result = await session.execute(
        select(Person).where(Person.telegram_user_id == telegram_user_id)
    )
    person = result.scalar_one_or_none()

    if person is None:
        person = Person(
            telegram_user_id=telegram_user_id,
            username=username,
            full_name=full_name,
            role="guest",
        )
        session.add(person)
    else:
        person.username = username
        person.full_name = full_name

    if phone is not None:
        person.phone = normalize_phone(phone)

    await session.flush()

    settings = get_settings()
    if settings.BOOTSTRAP_ADMIN_TELEGRAM_ID is not None:
        if telegram_user_id == settings.BOOTSTRAP_ADMIN_TELEGRAM_ID:
            person.role = "admin"

    await _activate_pending_admin_invites(session, person)

    await session.flush()
    return person


async def _activate_pending_admin_invites(
    session: AsyncSession, person: Person
) -> None:
    norm_username = _normalize_username(person.username)
    norm_phone = normalize_phone(person.phone)
    if norm_username is None and norm_phone is None:
        return

    result = await session.execute(
        select(AdminInvite).where(AdminInvite.status == "pending")
    )
    for invite in result.scalars().all():
        username_match = (
            norm_username is not None
            and _normalize_username(invite.username) == norm_username
        )
        phone_match = (
            norm_phone is not None and normalize_phone(invite.phone) == norm_phone
        )
        if not username_match and not phone_match:
            continue
        invite.status = "active"
        invite.person_id = person.id
        person.role = "admin"


async def has_pending_phone_invite(session: AsyncSession) -> bool:
    result = await session.execute(
        select(AdminInvite.id).where(
            AdminInvite.status == "pending",
            AdminInvite.phone.is_not(None),
        )
    )
    return result.first() is not None


async def invite_admin(
    session: AsyncSession,
    invited_by: Person,
    username: str | None,
    phone: str | None,
) -> AdminInvite:
    if invited_by.role != "admin":
        raise PermissionError("Only admins can invite admins")

    if not username and not phone:
        raise ValueError("username or phone is required")

    username = _normalize_username(username)
    phone = normalize_phone(phone)

    existing: Person | None = None
    if username:
        norm = _normalize_username(username)
        if norm:
            result = await session.execute(
                select(Person).where(
                    func.lower(func.ltrim(Person.username, "@")) == norm
                )
            )
            existing = result.scalar_one_or_none()
    if existing is None and phone:
        result = await session.execute(select(Person).where(Person.phone == phone))
        existing = result.scalar_one_or_none()

    if existing is not None:
        existing.role = "admin"
        invite = AdminInvite(
            username=username,
            phone=phone,
            invited_by_id=invited_by.id,
            status="active",
            person_id=existing.id,
        )
    else:
        invite = AdminInvite(
            username=username,
            phone=phone,
            invited_by_id=invited_by.id,
            status="pending",
        )

    session.add(invite)
    await session.flush()
    return invite


async def assign_client(session: AsyncSession, person_id: str) -> Person:
    result = await session.execute(select(Person).where(Person.id == person_id))
    person = result.scalar_one()
    if person.role != "admin":
        person.role = "client"
    await session.flush()
    return person
