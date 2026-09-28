"""Attendance application services."""

from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Attendance, DropInCharge, SchoolSession
from app.services.billing import active_subscription, current_price


def _session_local_date(ends_at, tz_name: str):
    if ends_at.tzinfo is None:
        raise ValueError("ends_at must be timezone-aware")
    return ends_at.astimezone(ZoneInfo(tz_name)).date()


async def _active_drop_in_product(session: AsyncSession):
    from app.models import Product

    result = await session.execute(
        select(Product).where(
            Product.kind == "drop_in",
            Product.active.is_(True),
        )
    )
    products = list(result.scalars().all())
    if not products:
        return None
    return products[0]


async def mark_attendance(
    session: AsyncSession,
    person_id: str,
    school_session_id: str,
    source: str,
) -> Attendance:
    if source not in ("admin", "client"):
        raise ValueError("source must be admin or client")

    result = await session.execute(
        select(Attendance).where(
            Attendance.person_id == person_id,
            Attendance.session_id == school_session_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.source == "admin" and source == "client":
            return existing
        if existing.source == source:
            return existing
        if existing.source == "client" and source == "admin":
            existing.source = "admin"
            await session.flush()
            return existing
        return existing

    attendance = Attendance(
        person_id=person_id,
        session_id=school_session_id,
        source=source,
    )
    session.add(attendance)
    await session.flush()

    session_result = await session.execute(
        select(SchoolSession).where(SchoolSession.id == school_session_id)
    )
    school_session = session_result.scalar_one()
    tz_name = get_settings().SCHOOL_TZ
    on_date = _session_local_date(school_session.ends_at, tz_name)

    sub = await active_subscription(session, person_id, on_date)
    if sub is not None:
        sub.lessons_left = max(0, sub.lessons_left - 1)
        await session.flush()
        return attendance

    charge_result = await session.execute(
        select(DropInCharge).where(
            DropInCharge.person_id == person_id,
            DropInCharge.session_id == school_session_id,
        )
    )
    if charge_result.scalar_one_or_none() is not None:
        return attendance

    drop_in = await _active_drop_in_product(session)
    if drop_in is None:
        raise ValueError("no active drop_in product")
    price = await current_price(session, drop_in.id, on_date)
    if price is None:
        raise ValueError("no drop_in price for session date")

    charge = DropInCharge(
        person_id=person_id,
        session_id=school_session_id,
        amount=price.amount,
        price_id=price.id,
        charged_at=school_session.ends_at,
    )
    session.add(charge)
    await session.flush()
    return attendance
