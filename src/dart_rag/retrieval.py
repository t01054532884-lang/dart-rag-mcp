"""하이브리드 검색: Qdrant 벡터 검색 + BM25 키워드 검색을 RRF로 합친다.

- 벡터: Qdrant 로컬 모드(파일 저장, 서버·Docker 불필요). 같은 코드로 Qdrant 서버 URL도 쓸 수 있다.
- 키워드: 한국어 형태소 분석기 없이 한글 단어의 글자 2-gram + 영문/숫자 토큰으로 BM25.
  "HBM", "2차전지" 같은 고유명사·숫자는 벡터보다 키워드 쪽이 잘 잡는다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from qdrant_client import QdrantClient, models

from .embedding import Embedder
from .store import Store

Mode = Literal["hybrid", "vector", "bm25"]
COLLECTION = "dart_chunks"
RRF_K = 60


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in re.findall(r"[가-힣]+|[a-z]+|\d+(?:\.\d+)?", text.lower()):
        if re.match(r"[가-힣]", word) and len(word) >= 2:
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        tokens.append(word)
    return tokens


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lens = np.array([len(d) for d in docs], dtype=float)
        self.avg = float(self.lens.mean()) if len(docs) else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.tfs))
        if not self.tfs:
            return out
        norm = self.k1 * (1 - self.b + self.b * self.lens / (self.avg or 1.0))
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            tf = np.array([d.get(term, 0) for d in self.tfs], dtype=float)
            out += idf * tf * (self.k1 + 1) / (tf + norm)
        return out


def chunk_header(corp_name: str, chunk: dict) -> str:
    sub = f" > {chunk['subtitle']}" if chunk.get("subtitle") else ""
    return f"[{corp_name}] {chunk['title']}{sub}"


@dataclass
class Hit:
    chunk_id: int
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


class HybridIndex:
    def __init__(self, store: Store, embedder: Embedder, qdrant: QdrantClient, collection: str = COLLECTION,
                 bm25_weight: float = 0.5):
        self.bm25_weight = bm25_weight
        self.store = store
        self.embedder = embedder
        self.qdrant = qdrant
        self.collection = collection
        self._bm25: BM25 | None = None
        self._bm25_ids: list[int] = []
        self._bm25_corp: np.ndarray = np.array([])
        self._bm25_section: np.ndarray = np.array([])

    # ------------------------------------------------------------ 색인
    def _doc_texts(self, chunks: list[dict]) -> list[str]:
        names = {c["corp_code"]: c["corp_name"] for c in self.store.companies()}
        return [f"{chunk_header(names.get(c['corp_code'], ''), c)}\n{c['text']}" for c in chunks]

    def build(self, batch_size: int = 64, progress: bool = False) -> int:
        chunks = self.store.all_chunks()
        if self.qdrant.collection_exists(self.collection):
            self.qdrant.delete_collection(self.collection)
        self.qdrant.create_collection(
            self.collection,
            vectors_config=models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE),
        )
        texts = self._doc_texts(chunks)
        for start in range(0, len(chunks), batch_size):
            part = chunks[start : start + batch_size]
            vecs = self.embedder.embed_documents(texts[start : start + batch_size])
            self.qdrant.upsert(
                self.collection,
                points=[
                    models.PointStruct(
                        id=c["chunk_id"],
                        vector=v,
                        payload={"corp_code": c["corp_code"], "section": c["section"], "rcept_no": c["rcept_no"]},
                    )
                    for c, v in zip(part, vecs)
                ],
            )
            if progress:
                print(f"  임베딩 {min(start + batch_size, len(chunks))}/{len(chunks)}", flush=True)
        stored = self.qdrant.count(self.collection, exact=True).count
        if stored != len(chunks):  # 예전 색인 점이 남아 있으면 검색 결과가 DB에 없는 청크를 가리킨다
            raise RuntimeError(f"색인 점 수({stored})가 청크 수({len(chunks)})와 다릅니다. 색인 폴더를 지우고 다시 만드세요.")
        self._bm25 = None
        return len(chunks)

    def _ensure_bm25(self) -> None:
        if self._bm25 is not None:
            return
        chunks = self.store.all_chunks()
        self._bm25 = BM25([tokenize(t) for t in self._doc_texts(chunks)])
        self._bm25_ids = [c["chunk_id"] for c in chunks]
        self._bm25_corp = np.array([c["corp_code"] for c in chunks])
        self._bm25_section = np.array([c["section"] for c in chunks])

    # ------------------------------------------------------------ 검색
    def _vector(self, query: str, corp_codes: list[str] | None, section: str | None, limit: int) -> list[int]:
        if not self.qdrant.collection_exists(self.collection):
            return []
        must = []
        if corp_codes:
            must.append(models.FieldCondition(key="corp_code", match=models.MatchAny(any=corp_codes)))
        if section:
            must.append(models.FieldCondition(key="section", match=models.MatchValue(value=section)))
        res = self.qdrant.query_points(
            self.collection,
            query=self.embedder.embed_query(query),
            query_filter=models.Filter(must=must) if must else None,
            limit=limit,
        )
        return [int(p.id) for p in res.points]

    def _keyword(self, query: str, corp_codes: list[str] | None, section: str | None, limit: int) -> list[int]:
        self._ensure_bm25()
        assert self._bm25 is not None
        scores = self._bm25.scores(tokenize(query))
        if corp_codes:
            scores = np.where(np.isin(self._bm25_corp, corp_codes), scores, 0.0)
        if section:
            scores = np.where(self._bm25_section == section, scores, 0.0)
        order = np.argsort(-scores)[:limit]
        return [self._bm25_ids[i] for i in order if scores[i] > 0]

    def search(
        self,
        query: str,
        corp_codes: list[str] | None = None,
        top_k: int = 5,
        mode: Mode = "hybrid",
        section: str | None = None,
        candidates: int = 50,
        bm25_weight: float | None = None,
    ) -> list[Hit]:
        """bm25_weight: 하이브리드에서 BM25 쪽 RRF 가중치(0~1). 벡터 쪽은 1 - bm25_weight. 기본 0.5(같은 비중)."""
        w_bm25 = self.bm25_weight if bm25_weight is None else bm25_weight
        weights = {"bm25": w_bm25, "vector": 1.0 - w_bm25}
        lists: dict[str, list[int]] = {}
        if mode in ("hybrid", "vector"):
            lists["vector"] = self._vector(query, corp_codes, section, candidates)
        if mode in ("hybrid", "bm25"):
            lists["bm25"] = self._keyword(query, corp_codes, section, candidates)
        fused: dict[int, Hit] = {}
        for name, ids in lists.items():
            for rank, cid in enumerate(ids, start=1):
                hit = fused.setdefault(cid, Hit(cid, 0.0))
                hit.score += (weights[name] if len(lists) > 1 else 1.0) / (RRF_K + rank)
                hit.ranks[name] = rank
        return sorted(fused.values(), key=lambda h: -h.score)[:top_k]
