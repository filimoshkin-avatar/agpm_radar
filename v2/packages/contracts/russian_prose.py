"""Language policy for generated prose; source titles are separate evidence."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
import unicodedata

RUSSIAN_PROSE_PROMPT = (
    "Язык: все пояснения, переводы, краткое содержание и выводы целиком по-русски, "
    "для читателя, который не знает языка первоисточника. Фразы на языке источника "
    "допускаются только в отдельном названии статьи (включая evidence_titles), "
    "не цитируй исходный заголовок в пересказе. Исключение — имена собственные: "
    "люди, компании, продукты и названия документов. Имена на нелатинской письменности "
    "передавай русской транскрипцией или подтверждённым латинским названием. "
    "Обычные термины и сокращения переводи: PMO — проектный офис, AI — ИИ, "
    "workflow — рабочий процесс, end-to-end workflows — сквозные рабочие процессы, "
    "human-in-the-loop — контроль со стороны человека. Не смешивай письменности "
    "внутри слова: вместо «整理ует» напиши «упорядочивает».\n"
)


def foreign_script_fragments(text: str) -> list[str]:
    """Reject untranslated scripts, including letters attached to Russian suffixes.

    Latin names remain available; names in other scripts use Russian transcription.
    This is a script check, not a semantic classifier of Latin proper names.
    """
    return [
        word
        for word in re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKC", text))
        if any(
            char.isalpha()
            and not ("а" <= char.casefold() <= "я" or char.casefold() == "ё")
            and "LATIN" not in unicodedata.name(char, "")
            for char in word
        )
    ]


def require_russian_script(text: str, field: str) -> None:
    if fragments := foreign_script_fragments(text):
        raise ValueError(
            f"RUSSIAN_PROSE_GATE: {field}: translate foreign script into Russian: "
            + ", ".join(fragments[:10])
        )


DOMAIN_PATTERN = r"\b([\w-]+)(?:\.[\w-]+)*\.(?:com|ai|io|org|net|ru|dev)\b"
LATIN_NAME_WORDS = frozenset({"and", "for", "the", "von", "van", "der", "del", "des", "of"})


def brand_words(*texts: str) -> frozenset[str]:
    """Source domains identify brands that conventionally use lowercase names."""
    return frozenset(
        match.group(1).lower() for text in texts for match in re.finditer(DOMAIN_PATTERN, text)
    )


def foreign_latin_words(text: str, allowed: frozenset[str] = frozenset()) -> list[str]:
    """Conservative lexical check, preserving capitalized names and source brands."""
    stripped = re.sub(DOMAIN_PATTERN, " ", text)
    found: list[str] = []
    previous_is_brand = False
    previous_end = 0
    for match in re.finditer(r"[A-Za-z][A-Za-z'’-]*", stripped):
        word = match.group().strip("-'’")
        follows_brand = previous_is_brand and stripped[previous_end : match.start()].isspace()
        if (
            len(word) >= 3
            and word.islower()
            and word not in LATIN_NAME_WORDS
            and word not in allowed
            and not follows_brand
            and word not in found
        ):
            found.append(word)
        previous_is_brand = word in allowed or follows_brand
        previous_end = match.end()
    return found


def require_russian_prose(text: str, field: str, allowed: frozenset[str] = frozenset()) -> None:
    require_russian_script(text, field)
    if words := foreign_latin_words(text, allowed):
        raise ValueError(
            f"RUSSIAN_PROSE_GATE: {field}: translate words outside proper names: "
            + ", ".join(words[:10])
        )
