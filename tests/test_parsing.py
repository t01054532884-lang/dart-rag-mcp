from pathlib import Path

from dart_rag.chunking import chunk_text
from dart_rag.parsing import extract_sections, to_text

XML = (Path(__file__).parent / "fixtures" / "sample_report.xml").read_text(encoding="utf-8")


def test_extracts_only_target_sections():
    secs = extract_sections(XML)
    assert [(s.section, s.subtitle) for s in secs] == [
        ("business", "1. 사업의 개요"),
        ("business", "2. 주요 제품 및 서비스"),
        ("mdna", "1. 예측정보에 대한 주의사항"),
    ]
    assert all("회사 개요는" not in s.text for s in secs)


def test_entities_and_tables_become_text():
    secs = extract_sections(XML)
    assert "HBM & DDR5" in secs[0].text
    table = secs[1].text.splitlines()
    assert table[0] == "구분 | 매출액 | 비중"
    assert table[1] == "DRAM | 1,000 | 70%"


def test_to_text_drops_empty_lines():
    assert to_text("<P> </P><P>가</P>") == "가"


def test_chunk_respects_max_and_overlaps():
    text = "\n".join(f"{i}번째 문단입니다. " * 5 for i in range(30))
    chunks = chunk_text(text, max_chars=200, overlap=40)
    assert all(len(c) <= 200 for c in chunks)
    assert len(chunks) > 5
    # 다음 청크가 앞 청크 끝부분을 이어받는다
    assert chunks[1].startswith(chunks[0][-40:])


def test_chunk_splits_long_paragraph():
    chunks = chunk_text("가" * 1000, max_chars=300, overlap=50)
    assert all(len(c) <= 300 for c in chunks)
    assert sum(len(c) for c in chunks) >= 1000


def test_keeps_angle_bracket_names_in_body():
    from dart_rag.parsing import to_text
    assert to_text("<P>당사의 <배틀그라운드> IP와 <SPAN USERMARK=\"B\">인조이</SPAN></P>") == "당사의 <배틀그라운드> IP와 인조이"
