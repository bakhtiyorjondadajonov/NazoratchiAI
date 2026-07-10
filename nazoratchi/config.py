"""Configuration models and YAML loading.

All tunables (thresholds, keyword tiers, allowlists, model ids) live in
config.yaml so they can be edited without touching code. SIGHUP re-loads
everything except model paths / worker counts, which need a restart.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)


class BotCfg(BaseModel):
    token_env: str = "GK_BOT_TOKEN"
    admin_chat_id: int
    admin_user_ids: list[int] = Field(min_length=1)

    @property
    def token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise RuntimeError(f"Bot token env var {self.token_env} is not set")
        return token


class ChatsCfg(BaseModel):
    # Optional SEED groups: auto-enabled at startup, reports go to the operator
    # chat. All other groups are self-serve via /enable.
    allowed: list[int] = []


class TenancyCfg(BaseModel):
    max_groups_per_owner: int = 20


class ModeCfg(BaseModel):
    # dry_run: never approve/decline/kick; every screening is reported to the
    # admin chat with the verdict the bot *would* have applied.
    dry_run: bool = False
    # DM the requester "your request is being reviewed" BEFORE processing
    # (the user_chat_id window closes once the request is processed).
    # Never includes detection reasons.
    notify_pending_user: bool = False


class QueueCfg(BaseModel):
    workers: int = 4
    flood_alert_depth: int = 25


class PhotosCfg(BaseModel):
    max_photos: int = 5
    download_retries: int = 2


class BellyComboCfg(BaseModel):
    belly_exposed_min: float = 0.50
    covered_class_margin: float = 0.10


class NudenetCfg(BaseModel):
    detector_model: str | None = None  # None = the 320n model bundled with the package
    classifier_enabled: bool = True
    classifier_model: str = "models/classifier_model.onnx"
    classifier_unsafe_threshold: float = 0.70
    inference_resolution: int = 320
    max_concurrent_inferences: int = 2
    intra_op_threads: int = 2
    decline: dict[str, float]
    hold: dict[str, float]
    belly_combo: BellyComboCfg = BellyComboCfg()
    ignore: list[str] = []


class GeminiCfg(BaseModel):
    enabled: bool = True
    model: str = "gemini-2.5-flash-lite"
    api_key_env: str = "GK_GEMINI_KEY"
    timeout_s: float = 10.0
    retries: int = 3
    temperature: float = 0.1
    circuit_breaker_failures: int = 5
    circuit_breaker_cooldown_s: float = 300.0

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


class LangKeywords(BaseModel):
    uz_latin: list[str] = []
    uz_cyrillic: list[str] = []
    ru: list[str] = []
    en: list[str] = []

    def all(self) -> list[str]:
        return [*self.uz_latin, *self.uz_cyrillic, *self.ru, *self.en]


class EmojiRulesCfg(BaseModel):
    hard: list[str] = ["\U0001f51e"]  # 🔞
    soft_combo_emojis: list[str] = []
    min_combo: int = 2  # this many combo emojis alone = soft signal


class TextCfg(BaseModel):
    hard_keywords: LangKeywords = LangKeywords()
    soft_keywords: LangKeywords = LangKeywords()
    emoji: EmojiRulesCfg = EmojiRulesCfg()
    hard_link_patterns: list[str] = []
    soft_link_patterns: list[str] = []
    leet_pass: bool = True


class LoggingCfg(BaseModel):
    dir: str = "logs"
    level: str = "INFO"


class AppConfig(BaseModel):
    bot: BotCfg
    chats: ChatsCfg = ChatsCfg()
    tenancy: TenancyCfg = TenancyCfg()
    mode: ModeCfg = ModeCfg()
    queue: QueueCfg = QueueCfg()
    photos: PhotosCfg = PhotosCfg()
    nudenet: NudenetCfg
    gemini: GeminiCfg = GeminiCfg()
    text: TextCfg = TextCfg()
    whitelist_users: list[int] = []
    db_path: str = "nazoratchi.db"
    logging: LoggingCfg = LoggingCfg()

    @field_validator("nudenet")
    @classmethod
    def _no_overlap(cls, v: NudenetCfg) -> NudenetCfg:
        overlap = set(v.decline) & set(v.hold)
        if overlap:
            raise ValueError(f"classes in both decline and hold tiers: {overlap}")
        overlap = (set(v.decline) | set(v.hold)) & set(v.ignore)
        if overlap:
            raise ValueError(f"classes both actioned and ignored: {overlap}")
        return v


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


class ConfigHolder:
    """Mutable holder so SIGHUP can swap the config while handlers keep a stable reference."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.current = load_config(self.path)

    def reload(self) -> bool:
        try:
            self.current = load_config(self.path)
            log.info("config reloaded from %s", self.path)
            return True
        except Exception:
            log.exception("config reload failed; keeping previous config")
            return False
