"""수집 CLI.

    python -m dart_rag.ingest all --year 2025          # 아래를 순서대로 전부
    python -m dart_rag.ingest companies                # 고유번호 목록 → 대상 회사 등록
    python -m dart_rag.ingest reports --year 2025      # 사업보고서 원문 → 장 추출 → 청크
    python -m dart_rag.ingest financials --years 2024 2025
    python -m dart_rag.ingest prices --start 2025-01-01
    python -m dart_rag.ingest index                    # 임베딩 → Qdrant
    python -m dart_rag.ingest stats
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from .chunking import chunk_text
from .config import ROOT, Settings, get_settings
from .dart_client import DartClient, DartError
from .parsing import extract_sections
from .store import Store

COMPANIES_FILE = ROOT / "config" / "companies.txt"


def read_targets(path: Path = COMPANIES_FILE) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        code, _, alias = line.partition(" ")
        out.append((code.strip(), alias.strip()))
    return out


def _client(s: Settings) -> DartClient:
    if not s.dart_api_key:
        sys.exit("DART_API_KEY가 없습니다. .env.example을 .env로 복사하고 키를 넣어 주세요.")
    return DartClient(s.dart_api_key)


def cmd_companies(s: Settings, store: Store) -> None:
    cache = s.raw_dir / "corp_codes.json"
    if cache.exists():
        corps = json.loads(cache.read_text(encoding="utf-8"))
    else:
        corps = _client(s).corp_codes()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(corps, ensure_ascii=False), encoding="utf-8")
    by_stock = {c["stock_code"]: c for c in corps}
    missing = []
    for code, alias in read_targets():
        c = by_stock.get(code)
        if c is None:
            missing.append(f"{code} {alias}")
            continue
        store.upsert_company(c["corp_code"], c["corp_name"], code, alias or None)
        print(f"  {code} {c['corp_name']} ({c['corp_code']})")
    if missing:
        print(f"고유번호 목록에 없는 종목코드: {missing}")


def _annual_report_candidates(filings: list[dict], year: int) -> list[dict]:
    """해당 연도 사업보고서 후보를 최신 순으로. [첨부정정]·[첨부추가]는 본문이 없어 뒤로 보낸다."""
    cands = [f for f in filings if "사업보고서" in f["report_nm"] and f"({year}." in f["report_nm"]]
    return sorted(cands, key=lambda f: ("첨부" in f["report_nm"], -int(f["rcept_no"])))  # 접수번호 = 날짜+일련번호


def _pick_annual_report(filings: list[dict], year: int) -> dict | None:
    cands = _annual_report_candidates(filings, year)
    return cands[0] if cands else None


def _main_xml(docs: dict[str, str], rcept_no: str) -> str:
    if f"{rcept_no}.xml" in docs:
        return docs[f"{rcept_no}.xml"]
    return max(docs.values(), key=len)  # 첨부서류보다 본문이 길다


def cmd_reports(s: Settings, store: Store, year: int, max_chars: int, overlap: int) -> None:
    client = _client(s)
    for corp in store.companies():
        filings = client.filings(corp["corp_code"], f"{year + 1}0101", f"{year + 1}1231")
        cands = _annual_report_candidates(filings, year)
        if not cands:
            print(f"  {corp['corp_name']}: {year}년 사업보고서 없음")
            continue
        f, xml = None, None
        for cand in cands:  # 최신 정정본부터, 원문 파일이 없으면 다음 후보
            cache = s.raw_dir / "documents" / f"{cand['rcept_no']}.xml"
            if cache.exists():
                f, xml = cand, cache.read_text(encoding="utf-8")
                break
            try:
                docs = client.document(cand["rcept_no"])
            except DartError as e:
                print(f"  {corp['corp_name']}: {cand['report_nm'].strip()} 원문 받기 실패 ({e}) → 다음 후보")
                continue
            if docs:
                f, xml = cand, _main_xml(docs, cand["rcept_no"])
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(xml, encoding="utf-8")
                break
        if f is None or xml is None:
            print(f"  {corp['corp_name']}: 원문 없음")
            continue
        rcept_no = f["rcept_no"]
        store.upsert_filing(rcept_no, corp["corp_code"], f["report_nm"].strip(), f["rcept_dt"], year)
        items = []
        for sec in extract_sections(xml):
            for i, text in enumerate(chunk_text(sec.text, max_chars, overlap)):
                items.append((sec.section, sec.title, sec.subtitle, i, text))
        n = store.replace_chunks(rcept_no, corp["corp_code"], items)
        print(f"  {corp['corp_name']}: {f['report_nm'].strip()} ({rcept_no}) → 청크 {n}개")


def cmd_financials(s: Settings, store: Store, years: list[int]) -> None:
    client = _client(s)
    for corp in store.companies():
        for y in years:
            msg = []
            for fs_div in ("CFS", "OFS"):
                rows = client.financial_statements(corp["corp_code"], y, fs_div=fs_div)
                if rows:
                    store.replace_financials(corp["corp_code"], y, fs_div, rows)
                    msg.append(f"{fs_div} {len(rows)}행")
            shares = client.share_counts(corp["corp_code"], y)
            if shares:
                store.replace_share_counts(corp["corp_code"], y, shares)
                msg.append("주식수")
            print(f"  {corp['corp_name']} {y}: {', '.join(msg) or '없음'}")


def cmd_prices(store: Store, start: str, end: str | None) -> None:
    import FinanceDataReader as fdr

    end = end or dt.date.today().isoformat()
    for corp in store.companies():
        df = fdr.DataReader(corp["stock_code"], start, end)
        rows = [
            (idx.strftime("%Y-%m-%d"), int(r.Open), int(r.High), int(r.Low), int(r.Close), int(r.Volume))
            for idx, r in df.iterrows()
        ]
        n = store.upsert_prices(corp["stock_code"], rows)
        print(f"  {corp['corp_name']}: {n}일")


def cmd_index(s: Settings, store: Store) -> None:
    from qdrant_client import QdrantClient

    from .embedding import FastEmbedder
    from .retrieval import HybridIndex

    print(f"임베딩 모델: {s.embed_model} (처음 한 번 내려받음)")
    embedder = FastEmbedder(s.embed_model)
    # Qdrant 로컬 모드의 delete_collection은 Windows에서 예전 점을 남길 때가 있어 폴더째 새로 만든다
    if s.qdrant_path.exists():
        shutil.rmtree(s.qdrant_path)
    s.qdrant_path.mkdir(parents=True, exist_ok=True)
    qdrant = QdrantClient(path=str(s.qdrant_path))
    try:
        n = HybridIndex(store, embedder, qdrant).build(progress=True)
    finally:
        qdrant.close()
    meta = {"embed_model": s.embed_model, "dim": embedder.dim, "chunks": n,
            "built_at": dt.datetime.now().isoformat(timespec="seconds")}
    (s.qdrant_path.parent / f"{s.qdrant_path.name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"색인 완료: 청크 {n}개")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dart-rag-ingest", description="OpenDART 수집·색인")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("companies")
    r = sub.add_parser("reports")
    r.add_argument("--year", type=int, required=True, help="사업연도 (예: 2025 → 2026년에 낸 사업보고서)")
    r.add_argument("--max-chars", type=int, default=900)
    r.add_argument("--overlap", type=int, default=150)
    f = sub.add_parser("financials")
    f.add_argument("--years", type=int, nargs="+", required=True)
    pr = sub.add_parser("prices")
    pr.add_argument("--start", required=True)
    pr.add_argument("--end")
    sub.add_parser("index")
    sub.add_parser("stats")
    a = sub.add_parser("all")
    a.add_argument("--year", type=int, required=True)
    a.add_argument("--price-start", default=None)
    args = p.parse_args(argv)

    s = get_settings()
    store = Store(s.db_path)
    try:
        if args.cmd == "companies":
            cmd_companies(s, store)
        elif args.cmd == "reports":
            cmd_reports(s, store, args.year, args.max_chars, args.overlap)
        elif args.cmd == "financials":
            cmd_financials(s, store, args.years)
        elif args.cmd == "prices":
            cmd_prices(store, args.start, args.end)
        elif args.cmd == "index":
            cmd_index(s, store)
        elif args.cmd == "all":
            print("[1/5] 회사"); cmd_companies(s, store)
            print("[2/5] 사업보고서"); cmd_reports(s, store, args.year, 900, 150)
            print("[3/5] 재무제표"); cmd_financials(s, store, [args.year - 1, args.year])
            print("[4/5] 주가"); cmd_prices(store, args.price_start or f"{args.year}-01-01", None)
            print("[5/5] 색인"); cmd_index(s, store)
        print(json.dumps(store.counts(), ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
