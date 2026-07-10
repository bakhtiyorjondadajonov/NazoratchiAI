"""Keyword / emoji / link screening of user-supplied text (bio + names + username).

Tiers:
- HARD: unambiguous adult-ad markers → TEXT_HARD signal (auto-decline tier).
  Requires a word-boundary match on a spaced variant (or a hard link/emoji),
  so obfuscated text can never auto-decline by itself.
- SOFT: ambiguous → TEXT_SOFT signal; the caller escalates to Gemini.
  Includes squashed-variant matches of ANY keyword (obfuscation like "s.e.x"),
  emoji combinations, suspicious links, mention-plus-signal, and heavy use of
  invisible characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gatekeeper.config import TextCfg
from gatekeeper.screening import textnorm

_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w{4,}")
_AGE_RE = re.compile(r"\b(?:18|21)\s*\+?\b")


@dataclass
class TextCheckResult:
    hard_hits: list[str] = field(default_factory=list)
    soft_hits: list[str] = field(default_factory=list)

    @property
    def needs_gemini(self) -> bool:
        return bool(self.soft_hits) and not self.hard_hits


class TextChecker:
    """Compiled form of the config keyword tiers. Rebuild after config reload."""

    def __init__(self, cfg: TextCfg):
        self.cfg = cfg
        self._hard_words = [textnorm.normalize_keyword(k) for k in cfg.hard_keywords.all()]
        self._soft_words = [textnorm.normalize_keyword(k) for k in cfg.soft_keywords.all()]
        self._hard_res = [self._word_re(k) for k in self._hard_words]
        self._soft_res = [self._word_re(k) for k in self._soft_words]
        self._squashed_all = [
            textnorm.squash(k) for k in (*self._hard_words, *self._soft_words)
            if len(textnorm.squash(k)) >= 4  # short stems in squashed text are FP factories
        ]
        self._hard_links = [re.compile(p, re.IGNORECASE) for p in cfg.hard_link_patterns]
        self._soft_links = [re.compile(p, re.IGNORECASE) for p in cfg.soft_link_patterns]

    @staticmethod
    def _word_re(keyword: str) -> re.Pattern[str]:
        return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)")

    def check(self, text: str, label: str) -> TextCheckResult:
        result = TextCheckResult()
        if not text or not text.strip():
            return result

        spaced, squashed = textnorm.variants(text, leet=self.cfg.leet_pass)
        raw_folded = " ".join(sorted(spaced))

        # keywords, word-boundary, on spaced variants
        for kw, kw_re in zip(self._hard_words, self._hard_res):
            if any(kw_re.search(v) for v in spaced):
                result.hard_hits.append(f"{label}: keyword '{kw}'")
        for kw, kw_re in zip(self._soft_words, self._soft_res):
            if any(kw_re.search(v) for v in spaced):
                result.soft_hits.append(f"{label}: keyword '{kw}'")

        # obfuscation tier: any keyword found inside separator-stripped text
        for kw in self._squashed_all:
            if any(kw in v for v in squashed):
                result.soft_hits.append(f"{label}: obfuscated '{kw}'")

        # emoji rules
        hard_emoji = [e for e in self.cfg.emoji.hard if e in text]
        for e in hard_emoji:
            result.hard_hits.append(f"{label}: emoji {e}")
        combo = [e for e in self.cfg.emoji.soft_combo_emojis if e in text]
        has_link = bool(_URL_RE.search(text))
        has_mention = bool(_MENTION_RE.search(text))
        has_age = bool(_AGE_RE.search(raw_folded))
        if combo and (len(combo) >= self.cfg.emoji.min_combo or has_link or has_mention or has_age):
            result.soft_hits.append(f"{label}: emoji combo {''.join(combo)}")

        # link patterns (checked on the raw casefolded text AND the latin-folded
        # variants, so Cyrillic-homoglyph domains are caught too)
        link_haystacks = [text.casefold(), raw_folded]
        for p in self._hard_links:
            if any(p.search(h) for h in link_haystacks):
                result.hard_hits.append(f"{label}: link pattern '{p.pattern}'")
        for p in self._soft_links:
            if any(p.search(h) for h in link_haystacks):
                result.soft_hits.append(f"{label}: link pattern '{p.pattern}'")

        # mention next to any sexual signal is bait ("write me @xxx")
        if has_mention and (combo or result.soft_hits or result.hard_hits):
            result.soft_hits.append(f"{label}: mention + signal")

        # invisible-character stuffing is itself suspicious
        if textnorm.count_invisible(text) > 2:
            result.soft_hits.append(f"{label}: {textnorm.count_invisible(text)} invisible chars")

        return result


def check_fields(checker: TextChecker, fields: dict[str, str | None]) -> TextCheckResult:
    """Run the checker over every non-empty field; merge results."""
    merged = TextCheckResult()
    for label, value in fields.items():
        if not value:
            continue
        r = checker.check(value, label)
        merged.hard_hits.extend(r.hard_hits)
        merged.soft_hits.extend(r.soft_hits)
    return merged
