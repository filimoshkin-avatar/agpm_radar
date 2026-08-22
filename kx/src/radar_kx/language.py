"""Which language a text is in, without pretending the answer is always known.

Defect D10: the detector this replaces counted Cyrillic characters against Latin
ones. Spanish came out `en`, Chinese came out `und`, and Ukrainian came out `ru`.
None of those is a small error in a store whose job is to say what a source
actually said.

Two stages, because they answer different questions and only one of them is hard:

**Script.** Most of the world's writing systems belong to one language for our
purposes. Han, Hangul, Kana, Thai, Georgian, Armenian, Hebrew answer themselves.
Arabic and Cyrillic and Latin do not.

**Function words.** Within Latin and within Cyrillic, the reliable dependency-free
signal is the frequency of a language's commonest words. It is a coarse
instrument - it needs a few hundred characters, and it will not tell Bokmal from
Nynorsk - but it is right about the cases D10 names, and it is honest when it is
unsure: two languages within a hair of each other returns the script's default
rather than a coin flip.

No new dependency. A proper library would be better at the tail, and this module
is deliberately shaped so one could replace `detect` without touching a caller -
but the locked requirements are a gate on this project, and buying the tail of
the distribution is not worth spending that gate on today.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

#: Below this many letters, nothing is claimed. The old detector used 40 and it
#: was too few: a 40-character run of function words decides nothing.
MIN_LETTERS = 120

#: Ideographic and syllabic scripts carry far more per character, so the same
#: confidence arrives sooner. 120 Han characters is a page; 30 is a paragraph and
#: already unambiguous about which script it is.
MIN_LETTERS_DENSE = 30
DENSE_SCRIPTS = frozenset({"han", "hiragana", "katakana", "hangul", "thai"})

#: How far ahead the winner must be, as a share of matched function words, before
#: the answer is the winner rather than the script's default.
MIN_MARGIN = 0.15

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Unicode script ranges we can name. Ordered so the first match wins; each entry
#: is (name, first, last).
_SCRIPT_RANGES = (
    ("latin", 0x0041, 0x024F),
    ("greek", 0x0370, 0x03FF),
    ("cyrillic", 0x0400, 0x052F),
    ("armenian", 0x0530, 0x058F),
    ("hebrew", 0x0590, 0x05FF),
    ("arabic", 0x0600, 0x06FF),
    ("devanagari", 0x0900, 0x097F),
    ("thai", 0x0E00, 0x0E7F),
    ("georgian", 0x10A0, 0x10FF),
    ("hangul", 0x1100, 0x11FF),
    ("hiragana", 0x3040, 0x309F),
    ("katakana", 0x30A0, 0x30FF),
    ("han", 0x4E00, 0x9FFF),
    ("hangul", 0xAC00, 0xD7AF),
)

#: Scripts that settle the question by themselves.
_SCRIPT_LANGUAGE = {
    "greek": "el",
    "armenian": "hy",
    "hebrew": "he",
    "devanagari": "hi",
    "thai": "th",
    "georgian": "ka",
    "hangul": "ko",
    "han": "zh",
    "hiragana": "ja",
    "katakana": "ja",
    "arabic": "ar",
}

#: Commonest function words, per language. Chosen to overlap as little as possible
#: between neighbours: "de" is in five of these lists and carries no information,
#: so what decides is the words that are not shared.
_FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "the of and to in that is was for it with as on are be this by from at".split()
    ),
    "de": frozenset(
        "der die und den von zu das mit sich des auf für ist nicht eine als auch".split()
    ),
    "fr": frozenset(
        "les des une dans est pour que qui par sur avec pas plus sont cette aux ses".split()
    ),
    "es": frozenset(
        "que los las del una por con para como más este sus pero son sobre entre".split()
    ),
    "pt": frozenset(
        "que não uma dos das com para como mais este seus mas são sobre pelo até".split()
    ),
    "it": frozenset(
        "che non una degli delle con per come più questo suoi sono sulla nel dal".split()
    ),
    "nl": frozenset(
        "het een van voor met zijn niet ook maar door worden deze naar wordt bij".split()
    ),
    "pl": frozenset(
        "nie jest że oraz przez dla które this został jako tego które są być może".split()
    ),
    "tr": frozenset(
        "bir bu ve için ile olarak daha çok olan kadar sonra gibi ancak ise ya".split()
    ),
    "sv": frozenset("och att det som för med den till inte har av kan vara sig men eller".split()),
    "cs": frozenset(
        "the není jsou které pro jako nebo ale tak když více jeho této byla svou".split()
    ),
    "ro": frozenset(
        "care este pentru din prin mai fost sunt către asupra dintre acest această".split()
    ),
    "id": frozenset(
        "yang dan untuk dengan dari pada ini tidak akan dalam adalah oleh atau".split()
    ),
    "vi": frozenset("của và các được trong người những cho một khi này đã là với".split()),
    "ru": frozenset("и в не на что с по для как это его к но от при или так же".split()),
    "uk": frozenset("та що для як це його або при має цього році також лише під між із".split()),
    "bg": frozenset("на за да се от при като този която което със които обаче тъй".split()),
    "sr": frozenset("је су на за да се од као овом који која које али тако него".split()),
    "kk": frozenset("және бұл үшін бойынша болып деп оның қазақ жылы туралы".split()),
}

#: Characters that decide a Cyrillic language, and characters that rule it out.
#:
#: Function words are the wrong instrument here: Russian and Ukrainian share most
#: of their commonest words, and a paragraph of Ukrainian matched one word in the
#: first attempt at this. The alphabets do not overlap - Ukrainian has і ї є ґ and
#: no ы э ъ, Russian the reverse - so the letters answer in one pass what a
#: word list answers badly in several.
_CYRILLIC_MARKERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "uk": (frozenset("іїєґ"), frozenset("ыэъё")),
    "ru": (frozenset("ыэёъ"), frozenset("іїєґўђћџ")),
    "bg": (frozenset("ъщ"), frozenset("ыэіїєё")),
    "sr": (frozenset("ђћџљњ"), frozenset("ыэіъ")),
    "kk": (frozenset("әғқңөұүһ"), frozenset("їєђћџ")),
}

#: Which languages compete inside a script, and which one wins a tie.
_SCRIPT_CANDIDATES = {
    "latin": (
        ("en", "de", "fr", "es", "pt", "it", "nl", "pl", "tr", "sv", "cs", "ro", "id", "vi"),
        "en",
    ),
    "cyrillic": (("ru", "uk", "bg", "sr", "kk"), "ru"),
}


@dataclass(frozen=True, slots=True)
class Detection:
    language: str
    script: str
    #: Share of the letters that belong to the winning script.
    script_share: float
    #: Share of matched function words that belong to the winning language, or
    #: ``None`` when the script decided on its own.
    confidence: float | None
    #: Why the answer is not more specific than it is.
    note: str | None = None


def _cyrillic(text: str, share: float, fallback: str) -> Detection:
    """Decide a Cyrillic language by its alphabet rather than by its word list."""
    lowered = text.casefold()
    counts = Counter(character for character in lowered if character.isalpha())
    scored: dict[str, int] = {}
    for language, (markers, against) in _CYRILLIC_MARKERS.items():
        for_it = sum(counts[character] for character in markers)
        against_it = sum(counts[character] for character in against)
        scored[language] = for_it - against_it
    best_language = max(scored, key=lambda name: scored[name])
    best = scored[best_language]
    if best <= 0:
        return Detection(fallback, "cyrillic", share, None, "no distinguishing letters")
    runner_up = max(value for name, value in scored.items() if name != best_language)
    total = sum(value for value in scored.values() if value > 0)
    if total and (best - max(runner_up, 0)) / total < MIN_MARGIN:
        return Detection(
            fallback, "cyrillic", share, best / total, f"{best_language} led by too little"
        )
    return Detection(best_language, "cyrillic", share, best / total if total else None)


def _script_of(character: str) -> str | None:
    point = ord(character)
    for name, first, last in _SCRIPT_RANGES:
        if first <= point <= last:
            return name
    return None


def detect(text: str) -> Detection:
    """Name the language of a text, or say ``und`` and why."""
    normalized = unicodedata.normalize("NFC", text)
    scripts: Counter[str] = Counter()
    for character in normalized:
        if not character.isalpha():
            continue
        name = _script_of(character)
        if name is not None:
            scripts[name] += 1
    letters = sum(scripts.values())
    if not letters:
        return Detection("und", "unknown", 0.0, None, "no letters")

    script, count = scripts.most_common(1)[0]
    share = count / letters
    minimum = MIN_LETTERS_DENSE if script in DENSE_SCRIPTS else MIN_LETTERS
    if letters < minimum:
        return Detection("und", "unknown", 0.0, None, f"only {letters} letters")

    # Kana beats Han: a Japanese text is mostly Han characters with kana between
    # them, and counting characters alone would call it Chinese.
    kana = scripts["hiragana"] + scripts["katakana"]
    if kana >= 0.02 * letters and scripts["han"]:
        return Detection("ja", "japanese", (kana + scripts["han"]) / letters, None)
    if script in _SCRIPT_LANGUAGE:
        return Detection(_SCRIPT_LANGUAGE[script], script, share, None)

    candidates, fallback = _SCRIPT_CANDIDATES[script]
    if script == "cyrillic":
        return _cyrillic(normalized, share, fallback)
    words = [word.casefold() for word in _WORD.findall(normalized)]
    hits = Counter(
        language for word in words for language in candidates if word in _FUNCTION_WORDS[language]
    )
    matched = sum(hits.values())
    if matched < 10:
        return Detection(fallback, script, share, None, f"only {matched} function words")
    ranked = hits.most_common(2)
    best_language, best_count = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    margin = (best_count - runner_up) / matched
    if margin < MIN_MARGIN:
        return Detection(
            fallback,
            script,
            share,
            best_count / matched,
            f"{best_language} led {ranked[1][0] if len(ranked) > 1 else '-'} by only {margin:.2f}",
        )
    return Detection(best_language, script, share, best_count / matched)


def language_of(text: str) -> str:
    """The label stored on a version. ``mixed`` when two scripts really share it."""
    detection = detect(text)
    if detection.language != "und" and 0 < detection.script_share < 0.7:
        return "mixed"
    return detection.language
