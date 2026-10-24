from aiogram import Router

from .billing import router as _billing_router
from .notices import router as _notices_router
from .people import router as _people_router
from .groups import (  # noqa: F401
    cb_once_cancel,
    fsm_week_notes,
    _apply_roster_replacements,
    _parse_lines_to_draft_dicts,
)
from .groups import router as _groups_router
from .legacy import (  # noqa: F401
    send_due_reminder,
    async_session_maker,
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
)
from .legacy import router as _legacy_router

from .legacy import *  # noqa: F401,F403

router = Router(name="telegram")
router.include_router(_billing_router)
router.include_router(_notices_router)
router.include_router(_people_router)
router.include_router(_groups_router)
router.include_router(_legacy_router)
