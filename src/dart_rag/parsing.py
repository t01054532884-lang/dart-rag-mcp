"""DART 공시 원본 XML(dart4.xsd 형식)에서 필요한 장을 뽑아 일반 텍스트로 바꾼다.

원본은 잘 짜인 XML이 아닐 때가 많아서(이스케이프 안 된 &, 깨진 태그) ElementTree 대신
정규식으로 SECTION/TITLE 경계만 찾는다.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

# 장 제목에 이 문자열이 들어 있으면 수집한다 → 저장할 때 쓰는 section 이름
TARGET_SECTIONS = {
    "사업의 내용": "business",
    "이사의 경영진단 및 분석의견": "mdna",
}

_FLAGS = re.S | re.I
_SECTION1 = re.compile(r"<SECTION-1\b.*?</SECTION-1>", _FLAGS)
_SECTION2 = re.compile(r"<SECTION-2\b.*?</SECTION-2>", _FLAGS)
_TITLE = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>", _FLAGS)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")  # 진짜 태그만. 본문의 <배틀그라운드> 같은 꺾쇠 이름은 남긴다


@dataclass(frozen=True)
class Section:
    section: str  # business / mdna
    title: str  # 장 제목 (예: "II. 사업의 내용")
    subtitle: str  # 소제목 (예: "1. 사업의 개요"), 없으면 ""
    text: str


def clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", raw))).strip()


def to_text(fragment: str) -> str:
    s = re.sub(r"<(TR)\b[^>]*>|</TR>", "\n", fragment, flags=re.I)
    s = re.sub(r"<(TD|TH|TE|TU)\b[^>]*>", " | ", s, flags=re.I)
    s = re.sub(r"<(P|BR|TITLE|SECTION-\d)\b[^>]*>|</(P|TITLE|SECTION-\d)>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    lines = []
    for line in s.splitlines():
        line = re.sub(r"[ \t 　]+", " ", line).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_sections(xml_text: str) -> list[Section]:
    out: list[Section] = []
    for block in _SECTION1.findall(xml_text):
        m = _TITLE.search(block)
        if not m:
            continue
        title = clean_title(m.group(1))
        compact = title.replace(" ", "")
        key = next((v for k, v in TARGET_SECTIONS.items() if k.replace(" ", "") in compact), None)
        if key is None:
            continue
        subs = _SECTION2.findall(block)
        if not subs:
            body = to_text(block[m.end():])
            if body:
                out.append(Section(key, title, "", body))
            continue
        for sub in subs:
            sm = _TITLE.search(sub)
            subtitle = clean_title(sm.group(1)) if sm else ""
            body = to_text(sub[sm.end():] if sm else sub)
            if body:
                out.append(Section(key, title, subtitle, body))
    return out
