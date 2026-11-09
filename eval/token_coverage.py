"""임베딩 모델이 청크를 얼마나 읽는지 (토큰 길이 제한 대비 실제 길이).

    python eval/token_coverage.py   → docs/token_coverage.json
fastembed가 내려받은 각 모델의 tokenizer.json으로 색인할 때와 같은 텍스트(헤더 + 본문)를 센다.
"""

import datetime as dt
import glob
import json
import os
import sqlite3
import statistics

from tokenizers import Tokenizer

from dart_rag.config import ROOT, get_settings
from dart_rag.retrieval import chunk_header

MODELS = {"paraphrase-multilingual-MiniLM-L12-v2": 128, "multilingual-e5-large": 512}
CACHE = os.path.join(os.environ.get("TEMP", "/tmp"), "fastembed_cache")


def main() -> None:
    db = sqlite3.connect(get_settings().db_path)
    db.row_factory = sqlite3.Row
    names = {r["corp_code"]: r["corp_name"] for r in db.execute("select * from companies")}
    texts = [f"{chunk_header(names[r['corp_code']], dict(r))}\n{r['text']}" for r in db.execute("select * from chunks")]
    rows = []
    for model, limit in MODELS.items():
        path = glob.glob(f"{CACHE}/*{model}*/snapshots/*/tokenizer.json")[0]
        tk = Tokenizer.from_file(path)
        tk.no_truncation()
        prefix = "passage: " if "e5" in model else ""
        n = [len(tk.encode(prefix + t).ids) for t in texts]
        rows.append({"model": model, "max_tokens": limit, "chunks": len(n), "median_tokens": statistics.median(n),
                     "over_limit_pct": round(sum(x > limit for x in n) / len(n) * 100, 1),
                     "avg_text_seen_pct": round(statistics.mean(min(1, limit / x) for x in n) * 100, 1)})
    out = ROOT / "docs" / "token_coverage.json"
    out.write_text(json.dumps({"rows": rows, "run_at": dt.datetime.now().isoformat(timespec="seconds")},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
