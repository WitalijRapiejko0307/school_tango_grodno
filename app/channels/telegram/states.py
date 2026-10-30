from aiogram.fsm.state import State, StatesGroup


class AdminInviteFSM(StatesGroup):
    waiting_identifier = State()


class ContactFSM(StatesGroup):
    name = State()
    phone = State()
    role_label = State()


class ContactEditFSM(StatesGroup):
    name = State()
    phone = State()
    role_label = State()


class NewGroupFSM(StatesGroup):
    name = State()
    new_stream_name = State()


class GroupStreamFSM(StatesGroup):
    new_stream_name = State()


class SlotFSM(StatesGroup):
    group_id = State()
    date_str = State()
    time_str = State()
    duration = State()
    place = State()
    bring_notes = State()


class GuestPickFSM(StatesGroup):
    number = State()


class WeekFSM(StatesGroup):
    time_str = State()
    duration = State()
    place = State()
    notes = State()
    once_date = State()


class AssignGroupFSM(StatesGroup):
    person_id = State()
    group_id = State()


class SubProductFSM(StatesGroup):
    name = State()
    audience = State()
    lessons = State()
    validity = State()
    price = State()


class DropInProductFSM(StatesGroup):
    name = State()
    price = State()


class OpenSubFSM(StatesGroup):
    person_id = State()
    product_id = State()
    starts_on = State()
    payment = State()
    partial_amount = State()


class NewPriceFSM(StatesGroup):
    product_id = State()
    amount = State()
    valid_from = State()


class ProductEditFSM(StatesGroup):
    name = State()
    audience = State()
    lessons = State()
    validity = State()


class AdminMenuFSM(StatesGroup):
    choosing = State()


class RosterFSM(StatesGroup):
    active = State()


class PeoplePickFSM(StatesGroup):
    waiting = State()
    card_name = State()
    card_phone = State()


class PayInFSM(StatesGroup):
    amount = State()


class SubEditFSM(StatesGroup):
    starts_on = State()
    valid_until = State()
    lessons_left = State()
    amount_due = State()
    confirm = State()


class SubDueFSM(StatesGroup):
    amount_due = State()


class PeopleAddFSM(StatesGroup):
    name = State()


class ListPickFSM(StatesGroup):
    waiting = State()
    notice_text = State()


class EnrollFSM(StatesGroup):
    waiting_name = State()


class NoticeFSM(StatesGroup):
    move_when = State()
    move_place = State()
    text = State()


class GroupRenameFSM(StatesGroup):
    waiting_name = State()


class TabelFSM(StatesGroup):
    date_from = State()
    date_to = State()


class PersonVisitFSM(StatesGroup):
    date = State()
    time = State()
    pick = State()
    edit_index = State()
    create_place = State()
    create_duration = State()


class GroupScheduleFSM(StatesGroup):
    date_from = State()
    date_to = State()
    move_when = State()
    move_place = State()
