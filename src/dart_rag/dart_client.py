"""OpenDART Open API 클라이언트.

쓰는 API (https://opendart.fss.or.kr/guide/main.do):
- corpCode.xml        : 고유번호 전체 목록 (zip)
- list.json           : 공시 검색
- fnlttSinglAcntAll.json : 단일회사 전체 재무제표
- stockTotqySttus.json   : 주식의 총수 현황
- document.xml        : 공시서류 원본 (zip)
"""

from __future__ import annotations

import io
import time
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import httpx

BASE_URL = "https://opendart.fss.or.kr/api"
NO_DATA = "013"  # 조회된 데이터가 없음

REPORT_CODES = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}


class DartError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(f"OpenDART 오류 {status}: {message}")
        self.status = status
        self.message = message


class DartClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        min_interval: float = 0.12,
        max_retries: int = 3,
    ):
        if not api_key:
            raise ValueError("DART_API_KEY가 비어 있습니다. .env에 넣어 주세요.")
        self._key = api_key
        self._http = client or httpx.Client(timeout=60.0)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_call = 0.0

    # ------------------------------------------------------------------ 기본 호출
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        query = {"crtfc_key": self._key, **(params or {})}
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self._http.get(f"{BASE_URL}/{path}", params=query)
            except httpx.TransportError:
                if attempt == self._max_retries:
                    raise
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 500 and attempt < self._max_retries:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def _json(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        data = self._get(path, params).json()
        status = data.get("status", "")
        if status == NO_DATA:
            return None
        if status != "000":
            raise DartError(status, data.get("message", ""))
        return data

    def _zip(self, path: str, params: dict[str, Any] | None = None) -> zipfile.ZipFile | None:
        content = self._get(path, params).content
        if content[:2] != b"PK":
            # 오류일 때는 zip 대신 <result><status>..</status> XML이 온다
            root = ET.fromstring(content)
            status = (root.findtext("status") or "").strip()
            if status == NO_DATA:
                return None
            raise DartError(status, (root.findtext("message") or "").strip())
        return zipfile.ZipFile(io.BytesIO(content))

    # ------------------------------------------------------------------ API
    def corp_codes(self) -> list[dict[str, str]]:
        """상장사(종목코드가 있는 회사)의 고유번호 목록."""
        zf = self._zip("corpCode.xml")
        if zf is None:
            return []
        return parse_corp_codes(zf.read(zf.namelist()[0]))

    def filings(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_detail_ty: str | None = "A001",
    ) -> list[dict[str, Any]]:
        """공시 목록. 기본값 A001 = 사업보고서."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "page_no": page,
                "page_count": 100,
            }
            if pblntf_detail_ty:
                params["pblntf_detail_ty"] = pblntf_detail_ty
            data = self._json("list.json", params)
            if data is None:
                break
            out.extend(data.get("list", []))
            if page >= int(data.get("total_page", 1)):
                break
            page += 1
        return out

    def financial_statements(
        self, corp_code: str, year: int, reprt_code: str = "11011", fs_div: str = "CFS"
    ) -> list[dict[str, Any]] | None:
        data = self._json(
            "fnlttSinglAcntAll.json",
            {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": fs_div},
        )
        return None if data is None else data.get("list", [])

    def share_counts(self, corp_code: str, year: int, reprt_code: str = "11011") -> list[dict[str, Any]] | None:
        data = self._json(
            "stockTotqySttus.json",
            {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code},
        )
        return None if data is None else data.get("list", [])

    def document(self, rcept_no: str) -> dict[str, str]:
        """공시 원본 zip 안의 XML 파일들을 {파일명: 텍스트}로."""
        zf = self._zip("document.xml", {"rcept_no": rcept_no})
        if zf is None:
            return {}
        return {name: decode_xml(zf.read(name)) for name in zf.namelist()}


def parse_corp_codes(xml_bytes: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        if not stock:
            continue
        out.append(
            {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "stock_code": stock,
            }
        )
    return out


def decode_xml(raw: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def dart_viewer_url(rcept_no: str) -> str:
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
