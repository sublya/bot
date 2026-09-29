from sublya.bot import texts
from sublya.bot.handlers import lang_name, stt_lang


def test_users_who_never_chose_get_the_default_language():
    # autodetection took Russian speech for English and translated it
    assert stt_lang(None, "ru") == "ru"


def test_explicit_choice_wins_over_the_default():
    assert stt_lang("en", "ru") == "en"


def test_auto_means_no_language_hint():
    assert stt_lang("auto", "ru") is None


def test_empty_default_means_autodetect():
    assert stt_lang(None, None) is None


def test_lang_names():
    assert lang_name(None, "ru") == texts.LANG_NAMES["ru"]
    assert lang_name("auto", "ru") == texts.LANG_AUTO
    assert lang_name("en", "ru") == texts.LANG_NAMES["en"]
    assert lang_name(None, None) == texts.LANG_AUTO
