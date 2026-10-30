"""SQLAlchemy ORM models for the tango school domain."""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Time,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _new_id() -> str:
    return str(uuid.uuid4())


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    telegram_user_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )
    viber_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="guest")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DeletedEntity(Base):
    """Snapshot of a permanently deleted archived entity.

    Rows that referenced the entity keep its id (no foreign key); the label here
    is what reports and lists show instead.
    """

    __tablename__ = "deleted_entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_deleted_entities_ref"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminInvite(Base):
    __tablename__ = "admin_invites"
    __table_args__ = (
        CheckConstraint(
            "username IS NOT NULL OR phone IS NOT NULL",
            name="ck_admin_invites_username_or_phone",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invited_by_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    person_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StreamMember(Base):
    __tablename__ = "stream_members"
    __table_args__ = (
        UniqueConstraint("stream_id", "person_id", name="uq_stream_members_stream_person"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    stream_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("streams.id"), nullable=False
    )
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stream_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("streams.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (Index("ix_group_memberships_person_id", "person_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class WeeklySlot(Base):
    """One row for a class that repeats every week. Concrete dates are derived, not stored."""

    __tablename__ = "weekly_slots"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "weekday",
            "start_time",
            name="uq_weekly_slots_group_weekday_start",
        ),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_weekly_slots_weekday"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    place: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SchoolSession(Base):
    __tablename__ = "school_sessions"
    __table_args__ = (
        UniqueConstraint(
            "weekly_slot_id",
            "session_date",
            name="uq_school_sessions_slot_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    weekly_slot_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    place: Mapped[str] = mapped_column(String(255), nullable=False)
    bring_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lessons_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    audience: Mapped[str] = mapped_column(String(16), nullable=False, default="person")


class Price(Base):
    __tablename__ = "prices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    product_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    partner_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    lessons_left: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_due: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    subscription_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("subscriptions.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    recorded_by_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attendance(Base):
    __tablename__ = "attendances"
    __table_args__ = (
        UniqueConstraint("person_id", "session_id", name="uq_attendances_person_session"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("school_sessions.id"), nullable=False
    )
    marked_by_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    marked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DropInCharge(Base):
    __tablename__ = "drop_in_charges"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "session_id", name="uq_drop_in_charges_person_session"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("school_sessions.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    charged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReminderResponse:
    """Values for Reminder.response (session intent and attendance answers)."""

    NONE = "none"
    COMING = "coming"
    DECLINED = "declined"
    ATTENDED_YES = "attended_yes"
    ATTENDED_NO = "attended_no"


class GuestRsvpStatus:
    PLANNED = "planned"
    REMINDED = "reminded"
    CANCELLED = "cancelled"


class BotChatMessage(Base):
    """Chat message kept so the bot can delete bot and user messages."""

    __tablename__ = "bot_chat_messages"
    __table_args__ = (Index("ix_bot_chat_messages_person_sent", "person_id", "sent_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    school_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("school_sessions.id"), nullable=True
    )
    is_reply_menu_anchor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("school_sessions.id"), nullable=True
    )
    subscription_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("subscriptions.id"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    nudge_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GuestRsvp(Base):
    __tablename__ = "guest_rsvps"
    __table_args__ = (
        UniqueConstraint("person_id", "session_id", name="uq_guest_rsvps_person_session"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("school_sessions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")


class DayGatherResponse:
    """Values for DayGather.response (morning RSVP)."""

    NONE = "none"
    COMING = "coming"
    DECLINED = "declined"


class DayGather(Base):
    __tablename__ = "day_gathers"
    __table_args__ = (
        UniqueConstraint(
            "person_id",
            "gather_date",
            name="uq_day_gathers_person_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    person_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    stream_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("streams.id"), nullable=True
    )
    gather_date: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    selected_session_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    nudge_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ContactCard(Base):
    __tablename__ = "contact_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    role_label: Mapped[str] = mapped_column(String(255), nullable=False)
    person_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
