"""Defect D10: the detector this replaces called Spanish English and Chinese nothing."""

from __future__ import annotations

import pytest

from radar_kx.language import MIN_LETTERS, detect, language_of

SAMPLES = {
    "en": (
        "The owner of an agentic run is accountable for the outcome, and the review "
        "is recorded in the decision log with a timestamp so that anyone can see who "
        "approved it and when. This is the practice that separates a governed run "
        "from an experiment."
    ),
    "es": (
        "El propietario de una ejecución agéntica es responsable del resultado, y la "
        "revisión queda registrada en el diario de decisiones con una marca de tiempo "
        "para que cualquiera pueda ver quién lo aprobó y cuándo. Esta es la práctica "
        "que separa una ejecución gobernada de un experimento."
    ),
    "fr": (
        "Le propriétaire d'une exécution agentique est responsable du résultat, et la "
        "revue est consignée dans le journal des décisions avec un horodatage pour que "
        "chacun puisse voir qui l'a approuvée et quand. C'est la pratique qui sépare "
        "une exécution gouvernée d'une expérience."
    ),
    "de": (
        "Der Eigentümer eines agentischen Laufs ist für das Ergebnis verantwortlich, "
        "und die Überprüfung wird mit einem Zeitstempel im Entscheidungsprotokoll "
        "festgehalten, damit jeder sehen kann, wer sie genehmigt hat und wann. Das ist "
        "die Praxis, die einen geregelten Lauf von einem Experiment trennt."
    ),
    "ru": (
        "Владелец агентного прогона отвечает за результат, и проверка записывается в "
        "журнал решений с отметкой времени, чтобы любой мог увидеть, кто и когда её "
        "утвердил. Именно эта практика отличает управляемый прогон от эксперимента."
    ),
    "uk": (
        "Власник агентного прогону відповідає за результат, і перевірка записується до "
        "журналу рішень з позначкою часу, щоб кожен міг побачити, хто та коли її "
        "затвердив. Саме ця практика відрізняє керований прогін від експерименту."
    ),
}


@pytest.mark.parametrize(("expected", "text"), SAMPLES.items())
def test_each_sample_is_named_correctly(expected: str, text: str) -> None:
    assert detect(text).language == expected


def test_spanish_is_no_longer_called_english() -> None:
    # The exact case D10 names. The old detector saw Latin letters and stopped.
    assert language_of(SAMPLES["es"]) == "es"
    assert language_of(SAMPLES["en"]) == "en"


def test_chinese_is_no_longer_called_undetermined() -> None:
    chinese = (
        "代理式项目管理要求每一次运行都有一位具名的负责人，并且审查结果必须记录在决策日志中。" * 3
    )
    assert detect(chinese).language == "zh"


def test_japanese_is_not_mistaken_for_chinese() -> None:
    # A Japanese text is mostly Han characters; counting characters alone loses.
    japanese = (
        "エージェント型のプロジェクト管理では、実行ごとに責任者を一人定める必要があります。" * 3
    )
    assert detect(japanese).language == "ja"


def test_ukrainian_is_not_folded_into_russian() -> None:
    assert detect(SAMPLES["uk"]).language == "uk"
    assert detect(SAMPLES["ru"]).language == "ru"


def test_a_text_too_short_to_judge_says_so_rather_than_guessing() -> None:
    detection = detect("Agentic project management.")
    assert detection.language == "und"
    assert "letters" in (detection.note or "")
    assert MIN_LETTERS > 40  # the old threshold decided nothing and said "en"


def test_a_cyrillic_text_with_no_distinguishing_letters_falls_back_and_says_why() -> None:
    # A coin flip in a store of evidence is worse than a coarse answer, so a text
    # that carries no letter unique to any one Cyrillic language gets the default
    # and a note saying that is what happened.
    neutral = "Проект начат вовремя, и команда работает над задачами по плану. " * 4
    detection = detect(neutral)
    assert detection.language == "ru"
    assert detection.note == "no distinguishing letters"


def test_too_few_function_words_falls_back_rather_than_deciding() -> None:
    latin = "Alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo. " * 4
    detection = detect(latin)
    assert detection.language == "en"
    assert "function words" in (detection.note or "")


def test_a_document_that_really_is_two_scripts_is_mixed() -> None:
    both = SAMPLES["ru"] + " " + SAMPLES["en"]
    assert language_of(both) == "mixed"


def test_the_detection_reports_what_it_leaned_on() -> None:
    detection = detect(SAMPLES["de"])
    assert detection.script == "latin"
    assert detection.script_share > 0.9
    assert detection.confidence is not None and detection.confidence > 0.4


def test_a_russian_text_with_a_hard_sign_is_not_bulgarian() -> None:
    # Measured on production: a Habr Telegram post came out `bg` because it
    # carried one ъ and no ы э ё. In Bulgarian ъ is an ordinary vowel running to
    # a percent or two of all letters; in Russian it appears once in a page.
    # Ordinary Russian prose: one hard sign in three hundred letters, and no
    # ы э ё at all, which is what made the production case tie.
    russian = (
        "Команда объявила о новой модели агента. Работа над задачами идёт по плану, "
        "и релиз намечен на конец месяца. Каждая проверка попадает в журнал решений "
        "с отметкой времени, чтобы любой участник видел, кто и когда её утвердил. "
        "Отдельно ведётся список открытых вопросов к следующей итерации."
    )
    assert detect(russian).language == "ru"


def test_bulgarian_is_still_recognised_by_how_often_it_uses_the_hard_sign() -> None:
    bulgarian = (
        "Първият български център за управление на проекти въведе нов подход към "
        "възлагането на задачи. Ръководителят на екипа съобщи, че всеки резултат "
        "трябва да бъде прегледан преди пускане. "
    ) * 3
    assert detect(bulgarian).language == "bg"
