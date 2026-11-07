"""Local embedding models. No API key, no network use after the first download."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from repomind.errors import RepoMindError


class Embedder(Protocol):
    model_name: str

    def embed_passages(self, texts: Sequence[str]) -> list[np.ndarray]: ...

    def embed_query(self, text: str) -> np.ndarray: ...


@dataclass(frozen=True)
class ModelSpec:
    name: str
    dim: int
    query_prefix: str = ""
    passage_prefix: str = ""
    hf_repo: str | None = None  # set for models fastembed does not ship in its catalogue
    model_file: str = "onnx/model.onnx"


MODEL_SPECS = {
    spec.name: spec
    for spec in (
        ModelSpec(
            "intfloat/multilingual-e5-small",
            dim=384,
            query_prefix="query: ",
            passage_prefix="passage: ",
            hf_repo="intfloat/multilingual-e5-small",
        ),
        ModelSpec("jinaai/jina-embeddings-v2-base-code", dim=768),
        ModelSpec(
            "BAAI/bge-small-en-v1.5",
            dim=384,
            query_prefix="Represent this sentence for searching relevant passages: ",
        ),
    )
}


class FastEmbedProvider:
    def __init__(self, model_name: str, models_dir: Path, batch_size: int = 16) -> None:
        spec = MODEL_SPECS.get(model_name)
        if spec is None:
            raise RepoMindError(
                f"Unknown embedding model: {model_name}",
                hint=f"Supported models: {', '.join(sorted(MODEL_SPECS))}",
            )
        self.spec = spec
        self.model_name = model_name
        self.batch_size = batch_size
        self._models_dir = models_dir
        self._model = None
        self._load_lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding
                from fastembed.common.model_description import ModelSource, PoolingType

                if self.spec.hf_repo and not any(
                    entry["model"] == self.model_name for entry in TextEmbedding.list_supported_models()
                ):
                    TextEmbedding.add_custom_model(
                        model=self.model_name,
                        pooling=PoolingType.MEAN,
                        normalization=True,
                        sources=ModelSource(hf=self.spec.hf_repo),
                        dim=self.spec.dim,
                        model_file=self.spec.model_file,
                    )
                self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(self._models_dir))
            except Exception as exc:
                raise RepoMindError(
                    f"Could not load embedding model {self.model_name}: {exc}",
                    hint="The first run needs internet access to download the model. Keyword search works without it.",
                ) from exc
            return self._model

    def embed_passages(self, texts: Sequence[str]) -> list[np.ndarray]:
        prefixed = [self.spec.passage_prefix + text for text in texts]
        return list(self._load().embed(prefixed, batch_size=self.batch_size))

    def embed_query(self, text: str) -> np.ndarray:
        return next(iter(self._load().embed([self.spec.query_prefix + text])))
