from app.services.user_labels import format_person_list_detail, format_person_role_label


def test_format_person_role_label() -> None:
    assert format_person_role_label("guest", False) == "гость"
    assert format_person_role_label("client", False) == "клиент"
    assert format_person_role_label("admin", False) == "админ"
    assert format_person_role_label("admin", True) == "админ, клиент"


def test_format_person_list_detail() -> None:
    assert (
        format_person_list_detail("Начинающие", "клиент", True)
        == "Начинающие — клиент — не оплачен"
    )
    assert format_person_list_detail("без группы", "гость", False) == "без группы — гость"
