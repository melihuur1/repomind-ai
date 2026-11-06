"""Optional local Turkish-to-English query translation.

Code is written in English, so a Turkish question shares almost no terms with it, and
even a multilingual embedding model loses precision across that gap. Translation is
imperfect, which is why the translated query is added to the original, never replaces it.
"""

from __future__ import annotations

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


class QueryTranslator(Protocol):
    def translate(self, text: str) -> str: ...


class LocalTurkishTranslator:
    """Opus-MT through CTranslate2: CPU only, no PyTorch, no network after the first download."""

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._translator = None
        self._source = None
        self._target = None

    def _load(self) -> None:
        if self._translator is not None:
            return
        try:
            import ctranslate2
            import sentencepiece
            from huggingface_hub import snapshot_download

            path = snapshot_download(MODEL_REPOSITORY, revision=MODEL_REVISION, cache_dir=str(self._models_dir))
            self._translator = ctranslate2.Translator(path, device="cpu", compute_type="int8")
            self._source = sentencepiece.SentencePieceProcessor(model_file=f"{path}/source.spm")
            self._target = sentencepiece.SentencePieceProcessor(model_file=f"{path}/target.spm")
        except Exception as exc:
            raise RepoMindError(f"Could not load the translation model: {exc}") from exc

    def translate(self, text: str) -> str:
        self._load()
        tokens = self._source.encode(text, out_type=str) + ["</s>"]
        result = self._translator.translate_batch([tokens], beam_size=4, max_decoding_length=64)
        output = [token for token in result[0].hypotheses[0] if token not in ("</s>", "<pad>")]
        return self._target.decode(output)
