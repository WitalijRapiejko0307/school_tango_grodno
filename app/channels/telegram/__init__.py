from aiogram import Router

from .billing import router as _billing_router
from .notices import router as _notices_router
from .people import router as _people_router
from .groups import router as _groups_router
from .core import router as _core_router

router = Router(name="telegram")
router.include_router(_billing_router)
router.include_router(_notices_router)
router.include_router(_people_router)
router.include_router(_groups_router)
router.include_router(_core_router)

# Re-exports kept for external callers (app/main.py, tests/).
from .core import (  # noqa: F401
    async_session_maker,
    reminder_inline_markup,
    reminder_reply_keyboard,
    reply_markup_for_role,
    role_keyboard,
    send_due_reminder,
)
from .groups import (  # noqa: F401
    _apply_roster_replacements,
    _parse_lines_to_draft_dicts,
    cb_once_cancel,
    fsm_week_notes,
)
