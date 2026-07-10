from nazoratchi.screening import textnorm


def test_zero_width_stripped():
    text = "s​e​x"
    assert textnorm.count_invisible(text) == 2
    assert textnorm.normalize_base(text) == "sex"


def test_bidi_controls_stripped():
    assert textnorm.normalize_base("ab‮cd‬") == "abcd"


def test_nfkc_and_casefold():
    assert textnorm.normalize_base("ＳＥＸ") == "sex"  # fullwidth
    assert textnorm.normalize_base("SeX") == "sex"


def test_uzbek_apostrophes_unified():
    for apo in ("ʻ", "ʼ", "‘", "’", "`", "´"):
        assert textnorm.normalize_base(f"o{apo}zbek") == "o'zbek"


def test_cyrillic_homoglyphs_fold_to_latin():
    # ѕех typed entirely in Cyrillic lookalikes
    spaced, _ = textnorm.variants("ѕех")
    assert "sex" in spaced


def test_latin_letters_fold_to_cyrillic():
    # "секс" with a Latin 'c' and Latin 'e' mixed in
    spaced, _ = textnorm.variants("cекс")  # c(lat) е(cyr) к(cyr) с(cyr)
    assert "секс" in spaced  # секс fully Cyrillic


def test_leet_variant():
    spaced, _ = textnorm.variants("s3x", leet=True)
    assert "sex" in spaced
    spaced_no_leet, _ = textnorm.variants("s3x", leet=False)
    assert "sex" not in spaced_no_leet


def test_squash_defeats_spacing_tricks():
    _, squashed = textnorm.variants("s.e.x")
    assert "sex" in squashed
    _, squashed = textnorm.variants("p o r n o")
    assert "porno" in squashed


def test_keyword_normalization_matches_text_normalization():
    assert textnorm.normalize_keyword("PORNO") == "porno"
    assert textnorm.normalize_keyword("Секс") == "секс"
