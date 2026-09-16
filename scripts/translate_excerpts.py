#!/usr/bin/env python3
"""분류 시트 논문 발췌 문장을 한국어로 옮긴다.

팀원이 영어 초록을 읽기 어렵다. labeling/cases.json의 발췌 문장만 Gemini로 번역해
labeling/excerpt_ko.json에 문장별로 저장한다. 이미 번역한 문장은 다시 보내지 않는다.
번역은 분류 시트 표시용이며 서비스 판정에는 쓰지 않는다.

    .venv/bin/python scripts/translate_excerpts.py
    .venv/bin/python scripts/build_labeling_cases.py   # 번역을 시트에 넣는다

주의: 무료 등급 Gemini는 보낸 내용이 제품 개선에 쓰인다. 공개된 논문 초록 문장만 보낸다.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from healthcheck.config import load_settings  # noqa: E402

CASES = ROOT / "labeling" / "cases.json"
CACHE = ROOT / "labeling" / "excerpt_ko.json"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BATCH = 40

PROMPT = """다음은 영어 의학·영양학 논문 초록에서 뽑은 문장들이다. 각 문장을 자연스러운 한국어로 번역하라.

규칙:
- 뜻을 더하거나 빼지 않는다. 특히 "유의하지 않았다", "관련이 없었다", "~할 수 있다", "동물 실험", "세포 실험" 같은
  결과의 강도와 연구 대상을 그대로 살린다
- 숫자, 단위, 통계값(p값, 신뢰구간 등)은 원문대로 둔다
- 성분·질환 이름은 흔히 쓰는 한국어 표기를 쓰고, 애매하면 괄호에 영어를 병기한다
- 입력과 같은 순서, 같은 개수의 문자열 배열(JSON)로만 답한다

문장:
"""


def translate(batch: list[str], api_key: str, model: str) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {sentence}" for i, sentence in enumerate(batch))
    body = {
        "contents": [{"parts": [{"text": PROMPT + numbered}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    for attempt in range(4):
        response = httpx.post(
            URL.format(model=model), json=body, headers={"x-goog-api-key": api_key}, timeout=120
        )
        if response.status_code in (429, 500, 503):
            time.sleep(15 * (attempt + 1))
            continue
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
        if isinstance(result, list) and len(result) == len(batch) and all(isinstance(r, str) for r in result):
            return result
        raise ValueError(f"번역 개수가 맞지 않습니다: 입력 {len(batch)} / 출력 {len(result) if isinstance(result, list) else '?'}")
    raise RuntimeError(f"Gemini 응답 {response.status_code}")


def main() -> int:
    settings = load_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY가 없습니다. .env를 확인하세요.")
        return 1
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    cache: dict[str, str] = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}
    sentences = sorted(
        {s for case in cases for claim in case["claims"] for paper in claim["papers"] for s in paper["excerpt"]["sentences"]}
    )
    todo = [s for s in sentences if s not in cache]
    print(f"발췌 문장 {len(sentences)}개, 번역할 문장 {len(todo)}개")

    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        try:
            for source, korean in zip(batch, translate(batch, settings.gemini_api_key, settings.gemini_model)):
                cache[source] = korean.strip()
        except (ValueError, RuntimeError, httpx.HTTPError) as error:
            print(f"⚠ {start + 1}~{start + len(batch)}번 실패: {str(error)[:120]}. 다음 실행 때 다시 시도합니다.")
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {min(start + BATCH, len(todo))}/{len(todo)}")
    missing = sum(1 for s in sentences if s not in cache)
    print(f"완료. 번역 없는 문장 {missing}개")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
