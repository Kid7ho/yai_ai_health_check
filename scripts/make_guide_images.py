#!/usr/bin/env python3
"""사용법 카드 사진을 만든다(1:1, 800×800).

실제 휴대폰 화면 캡처(2026-09-19, iPhone 다크 모드)에서 누를 곳만 잘라 빨간 표시를 넣는다.
다른 사람의 영상 썸네일·제목은 흐리게 가린다(특정 제품·영상을 예로 드는 것처럼 보이지 않게).
캡처 원본은 저장소에 두지 않는다. 사진을 바꾸면 step6_response.GUIDE_IMAGE_VERSION을 올린다.

    python3 scripts/make_guide_images.py <캡처 폴더>   # 2.webp(유튜브 공유), 3.webp(붙여넣기·결과), 4.webp(카톡 말풍선)

Pillow가 필요하다(시스템 python3).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "src" / "healthcheck" / "api" / "static" / "guide"
SIZE = 800
RED = (255, 59, 48)
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


def fit(crop: Image.Image, bg=(0, 0, 0), box: int = 740) -> Image.Image:
    scale = min(box / crop.width, box / crop.height)
    resized = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
    canvas = Image.new("RGB", (SIZE, SIZE), bg)
    canvas.paste(resized, ((SIZE - resized.width) // 2, (SIZE - resized.height) // 2))
    return canvas


def blur(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    region = img.crop(box).filter(ImageFilter.GaussianBlur(18))
    img.paste(region, box[:2])


def ring(draw: ImageDraw.ImageDraw, box, *, rounded: bool = False) -> None:
    if rounded:
        draw.rounded_rectangle(box, radius=28, outline=RED, width=9)
    else:
        draw.ellipse(box, outline=RED, width=9)


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    # AppleSDGothicNeo.ttc 안의 굵기 순서. 6이 Bold, 0이 Regular다.
    return ImageFont.truetype(FONT, size, index=6 if bold else 0)


def make(src: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    youtube = Image.open(src / "2.webp").convert("RGB")
    chat = Image.open(src / "3.webp").convert("RGB")
    received = Image.open(src / "4.webp").convert("RGB")

    # 1. 카톡으로 받은 영상: 말풍선 옆 [⤴]
    img = received.copy()
    blur(img, (250, 170, 900, 640))  # 남의 영상 썸네일과 제목
    draw = ImageDraw.Draw(img)
    ring(draw, (128, 405, 238, 515))
    fit(img.crop((40, 340, 640, 720))).save(OUT / "1_forward.png", optimize=True)

    # 2. 유튜브에서 봤다면: [공유] → [링크 복사]
    img = youtube.copy()
    draw = ImageDraw.Draw(img)
    ring(draw, (34, 1750, 350, 1878), rounded=True)
    fit(img.crop((20, 1320, 900, 1925))).save(OUT / "2_copy.png", optimize=True)

    # 3. 여기에 붙여 넣고 보내기
    img = chat.copy()
    draw = ImageDraw.Draw(img)
    ring(draw, (118, 1148, 770, 1252), rounded=True)
    ring(draw, (786, 1161, 898, 1273))
    fit(img.crop((20, 1105, 905, 1830))).save(OUT / "3_paste.png", optimize=True)

    # 4. 결과 카드
    fit(chat.crop((110, 340, 775, 1100))).save(OUT / "4_result.png", optimize=True)

    # 5. 이건 못 봐요 (그림)
    bg = (244, 243, 250)
    img = Image.new("RGB", (SIZE, SIZE), bg)
    draw = ImageDraw.Draw(img)
    rows = [("사진", 250), ("약 · 병원 이야기", 430)]
    for text, y in rows:
        draw.ellipse((110, y - 55, 220, y + 55), fill=RED)
        draw.line((140, y - 25, 190, y + 25), fill="white", width=12)
        draw.line((190, y - 25, 140, y + 25), fill="white", width=12)
        draw.text((260, y), text, font=font(68), fill=(23, 26, 36), anchor="lm")
    draw.text((SIZE // 2, 620), "이 결과는 참고용이며", font=font(38, bold=False), fill=(95, 99, 117), anchor="mm")
    draw.text((SIZE // 2, 672), "진단이 아닙니다.", font=font(38, bold=False), fill=(95, 99, 117), anchor="mm")
    img.save(OUT / "5_limits.png", optimize=True)


if __name__ == "__main__":
    make(Path(sys.argv[1]))
    print("만듦:", sorted(p.name for p in OUT.glob("*.png")))
