"""Pure helpers for person list/detail labels (Telegram UI)."""

from __future__ import annotations


def format_person_role_label(role: str, has_active_membership: bool) -> str:
    if role == "guest":
        return "гость"
    if role == "client":
        return "клиент"
    if role == "admin":
        if has_active_membership:
            return "админ, клиент"
        return "админ"
    return role


def format_person_list_detail(
    group_name: str, role_label: str, unpaid: bool
) -> str:
    parts = [group_name, role_label]
    if unpaid:
        parts.append("не оплачен")
    return " — ".join(parts)
