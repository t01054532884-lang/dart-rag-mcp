"""SQLite 저장소: 회사·공시·재무제표·주식수·주가·텍스트 청크."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    corp_code  TEXT PRIMARY KEY,
    corp_name  TEXT NOT NULL,
    stock_code TEXT NOT NULL UNIQUE,
    alias      TEXT                     -- config/companies.txt에 적은 짧은 이름 (예: 현대차)
);
CREATE TABLE IF NOT EXISTS filings (
    rcept_no   TEXT PRIMARY KEY,
    corp_code  TEXT NOT NULL REFERENCES companies(corp_code),
    report_nm  TEXT NOT NULL,
    rcept_dt   TEXT NOT NULL,
    bsns_year  INTEGER
);
CREATE TABLE IF NOT EXISTS financials (
    corp_code  TEXT NOT NULL,
    bsns_year  INTEGER NOT NULL,
    fs_div     TEXT NOT NULL,          -- CFS 연결 / OFS 별도
    sj_div     TEXT NOT NULL,          -- BS, IS, CIS, CF, SCE
    account_id TEXT,
    account_nm TEXT NOT NULL,
    ord        INTEGER,
    thstrm_amount INTEGER,             -- 당기 (원)
    frmtrm_amount INTEGER,             -- 전기 (원)
    currency   TEXT,
    rcept_no   TEXT
);
CREATE INDEX IF NOT EXISTS ix_fin ON financials(corp_code, bsns_year, fs_div);
CREATE TABLE IF NOT EXISTS share_counts (
    corp_code  TEXT NOT NULL,
    bsns_year  INTEGER NOT NULL,
    se         TEXT NOT NULL,          -- 보통주 / 우선주 / 합계
    issued     INTEGER,                -- 발행주식 총수
    treasury   INTEGER,                -- 자기주식수
    floating   INTEGER,                -- 유통주식수
    PRIMARY KEY (corp_code, bsns_year, se)
);
CREATE TABLE IF NOT EXISTS prices (
    stock_code TEXT NOT NULL,
    date       TEXT NOT NULL,
    open INTEGER, high INTEGER, low INTEGER, close INTEGER, volume INTEGER,
    PRIMARY KEY (stock_code, date)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY,
    rcept_no   TEXT NOT NULL,
    corp_code  TEXT NOT NULL,
    section    TEXT NOT NULL,
    title      TEXT NOT NULL,
    subtitle   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_corp ON chunks(corp_code);
"""


def parse_amount(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    # ------------------------------------------------------------ 쓰기
    def upsert_company(self, corp_code: str, corp_name: str, stock_code: str, alias: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO companies VALUES (?,?,?,?) ON CONFLICT(corp_code) DO UPDATE SET "
            "corp_name=excluded.corp_name, stock_code=excluded.stock_code, alias=excluded.alias",
            (corp_code, corp_name, stock_code, alias),
        )
        self.conn.commit()

    def upsert_filing(self, rcept_no: str, corp_code: str, report_nm: str, rcept_dt: str, bsns_year: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?)",
            (rcept_no, corp_code, report_nm, rcept_dt, bsns_year),
        )
        self.conn.commit()

    def replace_financials(self, corp_code: str, year: int, fs_div: str, rows: list[dict[str, Any]]) -> int:
        self.conn.execute(
            "DELETE FROM financials WHERE corp_code=? AND bsns_year=? AND fs_div=?", (corp_code, year, fs_div)
        )
        data = [
            (
                corp_code,
                year,
                fs_div,
                r.get("sj_div", ""),
                (r.get("account_id") or "").strip() or None,
                (r.get("account_nm") or "").strip(),
                parse_amount(r.get("ord")),
                parse_amount(r.get("thstrm_amount")),
                parse_amount(r.get("frmtrm_amount")),
                r.get("currency"),
                r.get("rcept_no"),
            )
            for r in rows
        ]
        self.conn.executemany("INSERT INTO financials VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def replace_share_counts(self, corp_code: str, year: int, rows: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM share_counts WHERE corp_code=? AND bsns_year=?", (corp_code, year))
        self.conn.executemany(
            "INSERT OR REPLACE INTO share_counts VALUES (?,?,?,?,?,?)",
            [
                (
                    corp_code,
                    year,
                    (r.get("se") or "").strip(),
                    parse_amount(r.get("istc_totqy")),
                    parse_amount(r.get("tesstk_co")),
                    parse_amount(r.get("distb_stock_co")),
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def upsert_prices(self, stock_code: str, rows: list[tuple[str, int, int, int, int, int]]) -> int:
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)", [(stock_code, *r) for r in rows]
        )
        self.conn.commit()
        return len(rows)

    def replace_chunks(self, rcept_no: str, corp_code: str, items: list[tuple[str, str, str, int, str]]) -> int:
        """items: (section, title, subtitle, seq, text)"""
        self.conn.execute("DELETE FROM chunks WHERE rcept_no=?", (rcept_no,))
        self.conn.executemany(
            "INSERT INTO chunks (rcept_no, corp_code, section, title, subtitle, seq, text) VALUES (?,?,?,?,?,?,?)",
            [(rcept_no, corp_code, *it) for it in items],
        )
        self.conn.commit()
        return len(items)

    # ------------------------------------------------------------ 읽기
    def companies(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM companies ORDER BY stock_code")

    def company_by_code(self, corp_code: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM companies WHERE corp_code=?", (corp_code,))
        return rows[0] if rows else None

    def filings(self, corp_code: str | None = None) -> list[dict[str, Any]]:
        if corp_code:
            return self._rows("SELECT * FROM filings WHERE corp_code=? ORDER BY rcept_dt DESC", (corp_code,))
        return self._rows("SELECT * FROM filings ORDER BY rcept_dt DESC")

    def financial_rows(self, corp_code: str, year: int, fs_div: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM financials WHERE corp_code=? AND bsns_year=? AND fs_div=? ORDER BY sj_div, ord",
            (corp_code, year, fs_div),
        )

    def available_years(self, corp_code: str) -> list[int]:
        return [r["bsns_year"] for r in self._rows(
            "SELECT DISTINCT bsns_year FROM financials WHERE corp_code=? ORDER BY bsns_year", (corp_code,)
        )]

    def share_counts(self, corp_code: str, year: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM share_counts WHERE corp_code=? AND bsns_year=?", (corp_code, year))

    def prices(self, stock_code: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM prices WHERE stock_code=?"
        params: list[Any] = [stock_code]
        if start:
            sql += " AND date>=?"
            params.append(start)
        if end:
            sql += " AND date<=?"
            params.append(end)
        return self._rows(sql + " ORDER BY date", params)

    def price_on_or_before(self, stock_code: str, date: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT * FROM prices WHERE stock_code=? AND date<=? ORDER BY date DESC LIMIT 1", (stock_code, date)
        )
        return rows[0] if rows else None

    def all_chunks(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM chunks ORDER BY chunk_id")

    def chunks_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["chunk_id"]: r for r in self._rows(f"SELECT * FROM chunks WHERE chunk_id IN ({marks})", ids)}

    def counts(self) -> dict[str, int]:
        tables = ["companies", "filings", "financials", "share_counts", "prices", "chunks"]
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
