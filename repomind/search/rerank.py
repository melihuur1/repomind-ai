"""Second-stage reranking with a local cross-encoder, which reads a query and a chunk together."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from repomind.errors import RepoMindError
from repomind.store.database import ChunkRow


class Reranker(Protocol):
    model_name: str

    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


def rerank_text(chunk: ChunkRow) -> str:
    """The path, symbol and signature carry meaning the code body alone may not state."""
    header = [f"File: {chunk.path}"]
    if chunk.symbol:
        header.append(f"{chunk.kind} {chunk.symbol}")
    if chunk.signature and chunk.signature != chunk.symbol:
        header.append(chunk.signature)
    return "\n".join(header) + "\n" + chunk.content


class CrossEncoderReranker:
    def __init__(self, model_name: str, models_dir: Path, batch_size: int = 16) -> None:
        self.model_name = model_name
        self._models_dir = models_dir
        self._batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._model = TextCrossEncoder(model_name=self.model_name, cache_dir=str(self._models_dir))
            except Exception as exc:
                raise RepoMindError(f"Could not load reranker {self.model_name}: {exc}") from exc
        return self._model

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        return [float(s) for s in self._load().rerank(query, list(documents), batch_size=self._batch_size)]
