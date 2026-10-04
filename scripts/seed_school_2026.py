"""Load the 01.09.2026 price list, three beginner weekly schedules, and the roster.

People are not placed in a group: the photo does not say who is in group 1, 2, or 3.
Safe to run again: existing product names, group names, and name keys are skipped.
"""

import asyncio
from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db import async_session_maker
from app.models import Group, Person, Product
from app.services.billing import create_product, set_price
from app.services.roster import name_key_from_parts
from app.services.schedule import create_group, create_weekly_slot, materialize_range

VALID_FROM = date(2026, 9, 1)

# kind, name, lessons, validity days, price
PRODUCTS: list[tuple[str, str, int | None, int | None, str]] = [
    ("drop_in", "1 занятие (55 мин), в группе, 1 человек", None, None, "18.00"),
    ("drop_in", "1 занятие (55 мин), в группе, семейная пара", None, None, "30.00"),
    ("drop_in", "1 занятие (55 мин), индивидуальное", None, None, "50.00"),
    ("drop_in", "1,5 занятия (1 час 25 мин), индивидуальное", None, None, "70.00"),
    ("drop_in", "2 занятия (1 час 55 мин), в группе, 1 человек", None, None, "35.00"),
    ("drop_in", "2 занятия (1 час 55 мин), в группе, семейная пара", None, None, "60.00"),
    ("subscription", "Абонемент на 4 занятия в месяц, 1 человек", 4, 28, "70.00"),
    ("subscription", "Абонемент на 4 занятия в месяц, семейная пара", 4, 28, "120.00"),
    ("subscription", "Абонемент на 8 занятий в месяц, 1 человек", 8, 28, "100.00"),
    ("subscription", "Абонемент на 8 занятий в месяц, семейная пара", 8, 28, "160.00"),
    ("subscription", "Абонемент на 12 занятий в месяц, 1 человек", 12, 28, "150.00"),
    ("subscription", "Абонемент на 12 занятий в месяц, семейная пара", 12, 28, "240.00"),
    ("subscription", "Абонемент на 16 занятий в месяц, 1 человек", 16, 28, "200.00"),
    ("subscription", "Абонемент на 16 занятий в месяц, семейная пара", 16, 28, "320.00"),
    ("drop_in", "Практика до 1,5 часа, Санаторная 1-13, пара", None, None, "15.00"),
]

# weekday: Monday 0 … Sunday 6. Friday 4, Saturday 5, Sunday 6.
SLOTS: dict[str, list[tuple[int, time, time, str, str | None]]] = {
    "1 группа": [
        (4, time(19, 0), time(20, 0), "ул. 1 Мая, 7 (3й этаж) у тренажерного зала", None),
        (5, time(18, 0), time(19, 0), "ул. 1 Мая, 7 (3й этаж) клуб Империя", None),
    ],
    "2 группа": [
        (4, time(19, 0), time(20, 0), "ул. 1 Мая, 7 (3й этаж) у тренажерного зала", "подгруппа 1"),
        (4, time(20, 0), time(21, 0), "ул. 1 Мая, 7 (3й этаж) у тренажерного зала", "подгруппа 2"),
        (6, time(20, 0), time(21, 0), "ул. 1 Мая, 7 (3й этаж) клуб Империя", None),
    ],
    "3 группа": [
        (6, time(16, 0), time(17, 0), "ул. 1 Мая, 7 (2й этаж) клуб Империя", None),
        (6, time(17, 0), time(18, 0), "ул. 1 Мая, 7 (2й этаж) клуб Империя", None),
    ],
}

NAMES = [
    "Асадчий Владимир",
    "Асадчий Ирина",
    "Богданович Надежда",
    "Вашкевич Ирина",
    "Вашкевич Сергей",
    "Винницкая Марина",
    "Винницкий Николай",
    "Волчкевич Татьяна",
    "Войтехович Олег",
    "Войтехович Елена",
    "Гулецкая Ирина",
    "Гулецкий Вячеслав",
    "Добренко Мария",
    "Гойлик Елена",
    "Капаченя Анна",
    "Кирпа Игорь",
    "Кисловская Ольга",
    "Козловская Дарья",
    "Колесникова Татьяна",
    "Кондратенко Любовь",
    "Курило Елена",
    "Летяго Лариса",
    "Марыкина Елена",
    "Мацкевич Татьяна",
    "Мулярчик Катя",
    "Опекун Светлана",
    "Пронько Анастасия",
    "Пронько Евгений",
    "Пронько Людмила",
    "Пронько Павел",
    "Рапейко Виталий",
    "Рассветаев Андрей",
    "Рахунок Ольга",
    "Рахунок Иван",
    "Савко Светлана",
    "Тырля Екатерина",
    "Федченко Нина",
    "Федченко Андрей",
    "Чирич Сако",
    "Шибутович Татьяна",
    "Шурман Людмила",
    "Гришкевич Светлана",
    "Сеченев Андрей",
    "Станкевич Александр",
    "Станкевич Елена",
    "Мордань Виктория",
    "Котусев Евгений",
    "Гусев Вячеслав",
    "Налейко Инга",
    "Мелешко Владислав",
]


async def main() -> None:
    async with async_session_maker() as session:
        products_added = 0
        for kind, name, lessons, validity, amount in PRODUCTS:
            existing = await session.scalar(select(Product).where(Product.name == name))
            if existing is not None:
                continue
            product = await create_product(session, kind, name, lessons, validity)
            await set_price(session, product.id, Decimal(amount), VALID_FROM)
            products_added += 1

        groups_added = 0
        slots_added = 0
        for group_name, slots in SLOTS.items():
            group = await session.scalar(select(Group).where(Group.name == group_name))
            if group is None:
                group = await create_group(session, group_name)
                groups_added += 1
            for weekday, start, end, place, notes in slots:
                from app.models import WeeklySlot

                found = await session.scalar(
                    select(WeeklySlot).where(
                        WeeklySlot.group_id == group.id,
                        WeeklySlot.weekday == weekday,
                        WeeklySlot.start_time == start,
                    )
                )
                if found is not None:
                    continue
                await create_weekly_slot(
                    session, group.id, weekday, start, end, place, notes
                )
                slots_added += 1

        today = date.today()
        await materialize_range(session, today, today + timedelta(days=21))

        people_added = 0
        for full_name in NAMES:
            surname, given = full_name.split(" ", 1)
            key = name_key_from_parts(surname, given)
            existing = await session.scalar(select(Person).where(Person.name_key == key))
            if existing is not None:
                continue
            session.add(
                Person(
                    full_name=full_name,
                    name_key=key,
                    role="client",
                    telegram_user_id=None,
                )
            )
            people_added += 1

        await session.commit()
        print(
            f"products_added {products_added} groups_added {groups_added} "
            f"slots_added {slots_added} people_added {people_added}"
        )


if __name__ == "__main__":
    asyncio.run(main())
