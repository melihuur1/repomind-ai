"""Optional local Turkish-to-English query translation.

Code is written in English, so a Turkish question shares almost no terms with it, and
even a multilingual embedding model loses precision across that gap. Translation is
imperfect, which is why the translated query is added to the original, never replaces it.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Protocol

from repomind.errors import RepoMindError
from repomind.search.tokenizer import _WORD, fold

MODEL_REPOSITORY = "gaudi/opus-mt-tr-en-ctranslate2"
MODEL_REVISION = "25595288827cf982178120a22f0a22f0f7b21dfc"

_TURKISH_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")
_TURKISH_WORDS = frozenset(
    fold(word)
    for word in """
    nasıl nerede neden niçin hangi kim ne zaman için ile ve veya bir bu şu olarak gibi kadar mi mı mu mü
    var yok değil ediliyor yapılıyor oluyor sağlıyor ise ama daha
    """.split()
)


def looks_turkish(text: str) -> bool:
    if any(character in _TURKISH_LETTERS for character in text):
        return True
    return any(fold(word) in _TURKISH_WORDS for word in _WORD.findall(text))


_TECHNICAL_TERM = re.compile(
    r"`[^`\n]+`"
    r"|[\w./-]*\w\.(?:java|tsx?|jsx?|sql|ya?ml|json|md|py)\b"
    r"|[\w.-]+/[\w./-]+"
    r"|\b[A-Za-z]+_\w+\b"
    r"|\b[a-z]+[A-Z]\w*\b"
    r"|\b[A-Z][a-z]+[A-Z]\w*\b"
    r"|\b[A-Z]{2,}\d*\b"
    # token-ttl, Retry-After, X-Request-Id, but not the Turkish word e-posta.
    r"|\b(?:[A-Z]|[A-Za-z][A-Za-z0-9]+)(?:-[A-Za-z0-9]+)+\b"
)


def keep_technical_terms(question: str, translation: str) -> str:
    """File paths, symbol names and acronyms must reach search as written, whatever a translator did to them."""
    terms = (term.strip("`") for term in dict.fromkeys(_TECHNICAL_TERM.findall(question)))
    missing = [term for term in terms if term.casefold() not in translation.casefold()]
    return f"{translation} {' '.join(missing)}" if missing else translation


_JOINER = re.compile(r"[_-]")


def plain_sentence(translation: str) -> str:
    """A model sometimes writes a whole sentence with underscores or hyphens where the spaces belong.

    Only a text with next to no spaces is changed, so an identifier inside a real sentence stays as written.
    """
    if translation.count(" ") <= 1 and len(_JOINER.findall(translation)) >= 3:
        return " ".join(_JOINER.sub(" ", translation).split())
    return translation


class QueryTranslator(Protocol):
    def translate(self, text: str) -> str: ...


class LocalTurkishTranslator:
    """Opus-MT through CTranslate2: CPU only, no PyTorch, no network after the first download."""

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._translator = None
        self._source = None
        self._target = None
        self._load_lock = threading.Lock()

    def _load(self) -> None:
        if self._translator is not None:
            return
        with self._load_lock:
            if self._translator is not None:
                return
            try:
                import ctranslate2
                import sentencepiece
                from huggingface_hub import snapshot_download

                path = snapshot_download(MODEL_REPOSITORY, revision=MODEL_REVISION, cache_dir=str(self._models_dir))
                self._source = sentencepiece.SentencePieceProcessor(model_file=f"{path}/source.spm")
                self._target = sentencepiece.SentencePieceProcessor(model_file=f"{path}/target.spm")
                # Last, because a thread that sees it set does not wait for this lock.
                self._translator = ctranslate2.Translator(path, device="cpu", compute_type="int8")
            except Exception as exc:
                raise RepoMindError(f"Could not load the translation model: {exc}") from exc

    def translate(self, text: str) -> str:
        self._load()
        tokens = self._source.encode(text, out_type=str) + ["</s>"]
        result = self._translator.translate_batch([tokens], beam_size=4, max_decoding_length=64)
        output = [token for token in result[0].hypotheses[0] if token not in ("</s>", "<pad>")]
        return self._target.decode(output)
