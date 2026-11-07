"""Code-aware tokenization shared by documents and queries.

``cancelReservation`` must match the query "cancel reservation" as well as the exact
identifier, so an identifier yields both its stemmed parts and its whole form. The same
goes for names joined by punctuation, such as ``token-ttl`` and ``STOCKGUARD_DB_HOST_PORT``:
the whole form is rare, which is what lets BM25 find the one line that holds it.
"""

from __future__ import annotations

import re
import threading
import unicodedata

import snowballstemmer

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_IDENTIFIER_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_JOINED_NAME = re.compile(r"[^\W_]+(?:[-_][^\W_]+)+", re.UNICODE)
_STEMMER = snowballstemmer.stemmer("english")
# The stemmer keeps state between calls, and the web server tokenizes from several threads.
_STEMMER_LOCK = threading.Lock()
_STEM_CACHE: dict[str, str] = {}

_STOPWORDS = frozenset(
    """
    a an and are as at be but by can could did do does for from had has have how i if in into is it its
    me my of on or our should so than that the their them then there these they this to was we were
    what when where which who why will with would you your
    ama bir bu da de daha en gibi hangi icin ile kadar mi mu nasil ne neden nerede o ve veya ya
    """.split()
)


def fold(word: str) -> str:
    """Case and diacritic folding that also handles the Turkish dotted and dotless i."""
    lowered = word.replace("İ", "i").replace("I", "i").replace("ı", "i").casefold()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stem(word: str) -> str:
    stem = _STEM_CACHE.get(word)
    if stem is None:
        with _STEMMER_LOCK:
            stem = _STEMMER.stemWord(word)
        _STEM_CACHE[word] = stem
    return stem


def _identifier_parts(word: str) -> list[str]:
    return _IDENTIFIER_PART.findall(word) if word.isascii() else [word]


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(text):
        parts = _identifier_parts(word)
        for part in parts:
            folded = fold(part)
            if folded and folded not in _STOPWORDS:
                tokens.append(_stem(folded))
        if len(parts) > 1:
            tokens.append(fold(word))
    tokens.extend(fold(name) for name in _JOINED_NAME.findall(text))
    return tokens
