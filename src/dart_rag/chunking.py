"""문단 경계를 살려 글자 수 기준으로 자르고, 앞 청크 끝부분을 겹쳐 붙인다."""

from __future__ import annotations


def _hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    step = max(1, max_chars - overlap)
    return [text[i : i + max_chars] for i in range(0, max(len(text) - overlap, 1), step)]


def chunk_text(text: str, max_chars: int = 900, overlap: int = 150) -> list[str]:
    if overlap >= max_chars:
        raise ValueError("overlap은 max_chars보다 작아야 합니다")
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        pieces = _hard_split(para, max_chars, overlap) if len(para) > max_chars else [para]
        for piece in pieces:
            candidate = f"{buf}\n{piece}" if buf else piece
            if len(candidate) <= max_chars:
                buf = candidate
                continue
            if buf:
                chunks.append(buf)
                tail = buf[-overlap:] if overlap else ""
                buf = f"{tail}\n{piece}" if tail and len(tail) + 1 + len(piece) <= max_chars else piece
            else:
                buf = piece
    if buf:
        chunks.append(buf)
    return chunks
