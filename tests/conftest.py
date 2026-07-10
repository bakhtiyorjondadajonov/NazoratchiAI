import pytest

from gatekeeper import routing
from gatekeeper.config import AppConfig, BotCfg, NudenetCfg


@pytest.fixture(autouse=True)
def _clear_routing_caches():
    routing._enabled_cache.clear()
    routing._dest_cache.clear()
    yield
    routing._enabled_cache.clear()
    routing._dest_cache.clear()


def make_config(**over) -> AppConfig:
    """Minimal valid AppConfig for tests."""
    base = dict(
        bot=BotCfg(admin_chat_id=-200, admin_user_ids=[999]),
        nudenet=NudenetCfg(
            decline={"FEMALE_BREAST_EXPOSED": 0.28},
            hold={"FEMALE_BREAST_COVERED": 0.40},
        ),
    )
    base.update(over)
    return AppConfig(**base)
