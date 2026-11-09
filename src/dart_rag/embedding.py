"""임베딩. 실제 실행은 fastembed(ONNX, GPU·PyTorch 불필요), 테스트는 해시 임베딩."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    """fastembed TextEmbedding 래퍼. e5 계열은 query:/passage: 접두어를 붙인다."""

    def __init__(self, model_name: str, batch_size: int = 32):
        from fastembed import TextEmbedding

        self.name = model_name
        self._model = TextEmbedding(model_name=model_name)
        self._batch = batch_size
        self._e5 = "e5" in model_name.lower()
        self.dim = len(self.embed_query("dimension probe"))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._e5:
            texts = [f"passage: {t}" for t in texts]
        return [v.tolist() for v in self._model.embed(texts, batch_size=self._batch)]

    def embed_query(self, text: str) -> list[float]:
        if self._e5:
            text = f"query: {text}"
        return next(iter(self._model.embed([text]))).tolist()


class HashEmbedder:
    """글자 2-gram을 해시해 만든 결정적 벡터. 모델 다운로드 없이 테스트할 때만 쓴다."""

    def __init__(self, dim: int = 256):
        self.name = f"hash-{dim}"
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        s = re.sub(r"\s+", "", text.lower())
        for i in range(len(s) - 1):
            h = int(hashlib.md5(s[i : i + 2].encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)
