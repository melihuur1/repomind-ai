"""Decides, without a model, whether a question is about something the code never mentions.

A small model asked "how does this repository use Kafka?" tends to invent an answer from
whatever it was shown, and similarity cannot help: unanswerable questions scored as high
as answerable ones (cosine 0.82-0.87 against 0.80-0.89). What does differ is vocabulary.
The topic of an unanswerable question simply does not occur in the code.

Generic verbs such as "prevent" are often missing too, even from answerable questions,
so a single ordinary missing word does not refuse a question. It only produces a warning.

A translation is weaker evidence still. Machine translation renders ordinary Turkish as
"retrialed" or "regulations", and on the development questions that refused three
answerable questions while catching two unanswerable ones. So words seen only in a
translation are reported and never refuse: those questions go on to the source reading,
where an answer still needs a quote that exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from repomind.search.tokenizer import _WORD, tokenize
from repomind.search.translation import looks_turkish

_ENOUGH_MISSING_WORDS = 2
_MIN_WORD_LENGTH = 2
_CONTRACTIONS = frozenset(
    "doesn don isn aren wasn weren won hasn haven hadn couldn shouldn wouldn didn".split()
)
_INTERNAL_CAPITAL = re.compile(r"[a-z0-9][A-Z]|[A-Z]{2}")
# Words that ask for a limit. Code writes a limit as max or min, as in @Max(3600) or MaxLength(100), so
# such a word is known when the code uses max or min. The Turkish glossary maps "en fazla" and "en az"
# the same way. On the development sets no unanswerable question was refused only for one of these.
_LIMIT_SENSES = {
    "max": frozenset({"longest", "largest", "biggest", "highest", "greatest", "maximum", "maximal"}),
    "min": frozenset({"shortest", "smallest", "lowest", "fewest", "minimum", "minimal"}),
}


@dataclass(frozen=True)
class Sufficiency:
    missing_names: tuple[str, ...]
    missing_words: tuple[str, ...]
    words_are_translated: bool = False

    @property
    def insufficient(self) -> bool:
        if self.missing_names:
            return True
        return not self.words_are_translated and len(self.missing_words) >= _ENOUGH_MISSING_WORDS

    @property
    def message(self) -> str | None:
        missing = dict.fromkeys((*self.missing_names, *self.missing_words))
        if not missing:
            return None
        return "Not found anywhere in the indexed code: " + ", ".join(missing) + "."


# What English adds to a word to make another of the same family, after stemming: show and shown,
# expose and exposure, create and creation. The stemmer joins "created" and "creates", and not these.
_FAMILY_ENDINGS = frozenset({"n", "en", "ion", "ation", "ur", "er", "or", "al", "ment", "iv", "anc", "enc", "ag", "abl", "ibl"})
_MIN_FAMILY_STEM = 4
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)?", re.IGNORECASE)


def _same_family(stem: str, vocabulary) -> bool:
    """Whether the code writes this word in another form of the same family.

    One stem has to be the start of the other, at least four letters long, and what is left has to be
    an ending English builds such forms with. So "exposed" is known where the code says "exposure",
    and "comment" is not known because the code says "commit".
    """
    if len(stem) < _MIN_FAMILY_STEM or not stem.isalpha():
        return False
    start = stem[:_MIN_FAMILY_STEM]
    for term in vocabulary:
        if not term.startswith(start) or term == stem:
            continue
        shorter, longer = sorted((stem, term), key=len)
        if longer.startswith(shorter) and longer[len(shorter):] in _FAMILY_ENDINGS:
            return True
    return False


def _known(word: str, vocabulary) -> bool:
    """A compound such as GraphQL counts as known only as a whole: "graph" alone says nothing."""
    tokens = tokenize(word)
    if not tokens or tokens[-1] in vocabulary:
        return True
    lowered = word.lower()
    if any(lowered in words and sense in vocabulary for sense, words in _LIMIT_SENSES.items()):
        return True
    return _same_family(tokens[-1], vocabulary)


def _looks_like_a_name(word: str, position: int) -> bool:
    """Kafka, GraphQL, S3. A capital at the start of a sentence says nothing by itself."""
    if _ORDINAL.fullmatch(word):
        return False  # 95th and 3 are numbers. S3 and OAuth2 are names.
    if any(character.isdigit() for character in word) and any(character.isalpha() for character in word):
        return True
    if _INTERNAL_CAPITAL.search(word):
        return True
    return position > 0 and word[0].isupper()


def check_sufficiency(question: str, translated: str | None, vocabulary) -> Sufficiency:
    names = [
        word
        for position, word in enumerate(_WORD.findall(question))
        if len(word) >= _MIN_WORD_LENGTH and _looks_like_a_name(word, position) and not _known(word, vocabulary)
    ]
    # Only English text can be compared with code. Without a translation, a Turkish
    # question has no comparable words, and only the names above can be checked.
    english = translated if translated else (None if looks_turkish(question) else question)
    words = []
    if english is not None:
        words = [
            word
            for word in _WORD.findall(english)
            if len(word) >= _MIN_WORD_LENGTH
            and word.lower() not in _CONTRACTIONS
            and not _ORDINAL.fullmatch(word)  # A number says how many, and is no part of the code's vocabulary.
            and not _known(word, vocabulary)
        ]
    words = [word for word in dict.fromkeys(words) if word not in names]
    return Sufficiency(tuple(dict.fromkeys(names)), tuple(words), words_are_translated=bool(translated))
