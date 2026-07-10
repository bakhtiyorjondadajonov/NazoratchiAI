"""Text normalization against obfuscation.

Adversaries mix Cyrillic/Latin homoglyphs (Uzbek itself is written in both
scripts), insert zero-width characters, use leetspeak and spacing tricks.
`variants()` produces the set of normalized strings a keyword should be
matched against:

- spaced variants: word structure preserved (for word-boundary matches)
- squashed variants: all separators removed (catches "s.e.x", "s e x" —
  matches here are lower-confidence and should only count as soft signals)
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width / bidi / invisible characters used to split keywords invisibly.
_INVISIBLE = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LRM/RLM
    " -‮"  # line/para separators, bidi embedding/overrides
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # bidi isolates
    "﻿"         # BOM / zero-width no-break space
    "­"         # soft hyphen
    "؜"         # arabic letter mark
    "︀-️"  # variation selectors
    "]"
)

# Uzbek Latin uses several apostrophe-like characters interchangeably (oʻ o' o`).
_APOSTROPHES = str.maketrans({c: "'" for c in "ʻʼ‘’`´′"})

# Visually identical lowercase pairs. Folding is applied in BOTH directions so
# a keyword list in either script matches mixed-script text.
_CYR_TO_LAT = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h",
    "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x",
    "і": "i", "ї": "i", "ѕ": "s", "ј": "j", "һ": "h", "ԛ": "q", "ԝ": "w",
})
_LAT_TO_CYR = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "h": "һ",
})

_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "@": "a", "$": "s", "!": "i", "€": "e", "£": "l",
})

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def count_invisible(text: str) -> int:
    return len(_INVISIBLE.findall(text))


def normalize_base(text: str) -> str:
    """Unify apostrophes → NFKC → casefold → strip invisible → split on _ → collapse spaces.

    Apostrophes come first because NFKC decomposes some of them (´ → space +
    combining acute). Underscores become spaces so keywords match inside
    usernames like "lolita_porno".
    """
    text = text.translate(_APOSTROPHES)
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _INVISIBLE.sub("", text)
    text = text.replace("_", " ")
    return _SPACES.sub(" ", text).strip()


def squash(text: str) -> str:
    """Remove every separator/punctuation character (defeats spacing tricks)."""
    return _NON_WORD.sub("", text)


def variants(text: str, leet: bool = True) -> tuple[set[str], set[str]]:
    """Return (spaced_variants, squashed_variants) for keyword matching."""
    base = normalize_base(text)
    spaced = {base, base.translate(_CYR_TO_LAT), base.translate(_LAT_TO_CYR)}
    if leet:
        spaced |= {v.translate(_LEET) for v in set(spaced)}
    spaced.discard("")
    squashed = {squash(v) for v in spaced}
    squashed.discard("")
    return spaced, squashed


def normalize_keyword(keyword: str) -> str:
    """Keywords go through the same base normalization as the text they match."""
    return normalize_base(keyword)
