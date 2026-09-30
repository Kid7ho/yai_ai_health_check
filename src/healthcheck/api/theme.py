"""어르신용 색 테마(2026-10-01 사용자 결정). 포스터·공유 화면·안내 페이지·교육 자료가 같은 색을 쓴다.

노랑은 면(버튼·강조·번호 원)에만 쓰고 글씨로는 쓰지 않는다(흰 바탕 노란 글씨는 대비 1.3:1).
글씨는 진한 갈색. 파랑·보라는 나이 든 눈에 어둡고 비슷하게 보여 핵심 정보에 쓰지 않는다.
주황은 '조심하세요' 신호등과 겹치므로 신호등이 아닌 곳의 강조 글씨는 진한 주황(ACCENT_TEXT)을 쓴다.
"""

from __future__ import annotations

import html

YELLOW = "#FEE500"  # 포인트(면) — 진한 갈색 글씨와 대비 약 14:1
BG = "#FFF8EC"  # 크림 바탕
INK = "#2B2118"  # 글씨 — 크림 바탕과 대비 약 15:1
MUTED = "#6B5B4B"  # 보조 글씨 — 대비 약 6:1, 큰 글씨에만
LINE = "#EFE3CF"
BAND = "#3A2A1E"  # 짙은 띠 — 노란 글씨와 대비 약 11:1
ACCENT_TEXT = "#B45309"  # 강조 글씨 — 대비 약 4.8:1
SIGNAL = {"TRUSTWORTHY": "#2E9E5B", "CAUTION": "#F59E0B", "WARNING": "#E0453A", "UNKNOWN": "#8E8A94"}
FONT = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", sans-serif'


def simple_page(title: str, paragraphs: list[str]) -> str:
    """안내만 담은 작은 페이지(채널 안내 등). 판정 결과가 아니므로 면책 문구를 붙이지 않는다."""
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>html,body{{margin:0;background:{BG};color:{INK};font-family:{FONT}}}
main{{max-width:480px;margin:0 auto;padding:40px 24px;font-size:22px;line-height:1.6}}
.brand{{display:inline-block;background:{YELLOW};border-radius:999px;padding:6px 18px;font-weight:800;font-size:20px}}
h1{{font-size:32px;margin:18px 0 8px}} p{{color:{MUTED}}}</style></head>
<body><main><span class="brand">AI 건강지킴이</span><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
