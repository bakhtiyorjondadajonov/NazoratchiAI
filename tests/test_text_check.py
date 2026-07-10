import pytest

from gatekeeper.config import EmojiRulesCfg, LangKeywords, TextCfg
from gatekeeper.screening.text_check import TextChecker, check_fields


@pytest.fixture
def checker() -> TextChecker:
    cfg = TextCfg(
        hard_keywords=LangKeywords(
            en=["porno", "onlyfans", "escort"],
            ru=["эскорт", "порно"],  # эскорт, порно
            uz_latin=["jalab"],
            uz_cyrillic=["жалаб"],  # жалаб
        ),
        soft_keywords=LangKeywords(
            en=["sex", "nudes"],
            ru=["интим"],  # интим
            uz_latin=["seks"],
        ),
        emoji=EmojiRulesCfg(
            hard=["\U0001f51e"],  # 🔞
            soft_combo_emojis=["\U0001f351", "\U0001f4a6", "\U0001f346"],  # 🍑💦🍆
            min_combo=2,
        ),
        hard_link_patterns=[r"onlyfans\.", r"fansly\."],
        soft_link_patterns=[r"t\.me/\+", r"linktr\.ee", r"bit\.ly"],
        leet_pass=True,
    )
    return TextChecker(cfg)


def test_clean_bio_passes(checker):
    r = checker.check("Love hiking, books and coffee", "bio")
    assert not r.hard_hits and not r.soft_hits


def test_uzbek_clean_bio_passes(checker):
    r = checker.check("Farg'onalik dasturchi. Kitob o'qishni yaxshi ko'raman", "bio")
    assert not r.hard_hits and not r.soft_hits


def test_hard_keyword_english(checker):
    assert checker.check("best porno channel", "bio").hard_hits


def test_hard_keyword_russian(checker):
    assert checker.check("эскорт услуги", "bio").hard_hits


def test_hard_keyword_uzbek_both_scripts(checker):
    assert checker.check("jalab kanal", "bio").hard_hits
    assert checker.check("жалаб канал", "bio").hard_hits


def test_mixed_script_homoglyph_still_hard(checker):
    # "pоrnо" with Cyrillic о — folds back to Latin "porno"
    assert checker.check("pоrnо", "bio").hard_hits


def test_word_boundary_no_false_positive(checker):
    # "sex" must not fire inside "sussex"; "porno" not inside e.g. a longer word
    r = checker.check("Born in Sussex, England", "bio")
    assert not r.hard_hits
    assert not any("keyword" in h for h in r.soft_hits)


def test_obfuscated_keyword_is_soft_not_hard(checker):
    r = checker.check("p.o.r.n.o here", "bio")
    assert not r.hard_hits  # obfuscation can never auto-decline
    assert any("obfuscated" in h for h in r.soft_hits)


def test_soft_keyword_escalates(checker):
    r = checker.check("seks haqida", "bio")
    assert r.soft_hits and not r.hard_hits
    assert r.needs_gemini


def test_hard_hit_suppresses_gemini(checker):
    r = checker.check("porno and seks", "bio")
    assert r.hard_hits
    assert not r.needs_gemini


def test_emoji_18_hard(checker):
    assert checker.check("\U0001f51e content", "bio").hard_hits


def test_single_soft_emoji_alone_passes(checker):
    r = checker.check("\U0001f351 peach farmer", "bio")
    assert not r.hard_hits and not r.soft_hits


def test_emoji_combo_is_soft(checker):
    r = checker.check("\U0001f351\U0001f4a6 write me", "bio")
    assert any("emoji combo" in h for h in r.soft_hits)


def test_emoji_plus_link_is_soft(checker):
    r = checker.check("\U0001f351 t.me/mychannel", "bio")
    assert any("emoji combo" in h for h in r.soft_hits)


def test_emoji_plus_age_is_soft(checker):
    r = checker.check("\U0001f351 18+", "bio")
    assert any("emoji combo" in h for h in r.soft_hits)


def test_hard_link(checker):
    assert checker.check("see onlyfans.com/someone", "bio").hard_hits


def test_soft_links(checker):
    assert checker.check("join t.me/+AbCdEf", "bio").soft_hits
    assert checker.check("linktr.ee/me", "bio").soft_hits


def test_invisible_stuffing_is_soft(checker):
    r = checker.check("h​i​ ​there", "bio")
    assert any("invisible" in h for h in r.soft_hits)


def test_check_fields_covers_names(checker):
    merged = check_fields(checker, {
        "bio": None,
        "first_name": "Lola \U0001f51e",
        "last_name": None,
        "username": "lolita_porno",
    })
    assert any("first_name" in h for h in merged.hard_hits)
    assert any("username" in h for h in merged.hard_hits)
