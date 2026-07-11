"""Catalog integrity: EN/UZ key parity, placeholder parity, safe fallback."""

import string

import pytest

from nazoratchi.strings import _STR, DEFAULT_LANG, LANGS, t


def placeholders(value: str) -> set:
    return {field for _, field, _, _ in string.Formatter().parse(value) if field}


def test_key_parity_between_languages():
    assert set(_STR["en"]) == set(_STR["uz"])


def test_placeholder_parity_between_languages():
    """Every key must take the same format kwargs in both languages, or a
    localized t(...) call would raise KeyError at runtime."""
    for key in _STR["en"]:
        assert placeholders(_STR["en"][key]) == placeholders(_STR["uz"][key]), key


def test_default_lang_is_valid():
    assert DEFAULT_LANG in LANGS and set(LANGS) == set(_STR)


def test_unknown_language_falls_back_to_english():
    assert t("de", "verdict.decline") == t("en", "verdict.decline")


def test_missing_key_in_lang_falls_back_to_english(monkeypatch):
    monkeypatch.delitem(_STR["uz"], "verdict.decline")
    assert t("uz", "verdict.decline") == t("en", "verdict.decline")


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        t("en", "no.such.key")


def test_format_kwargs_applied():
    assert "3" in t("uz", "enable.cap", n=3)
    assert "@bot" not in t("en", "enable.no_dm", bot="mybot")
    assert "mybot" in t("en", "enable.no_dm", bot="mybot")
