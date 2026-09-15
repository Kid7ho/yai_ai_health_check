"""자막이 없는 영상을 Gemini 영상 이해로 글로 옮긴다.

자막이 있으면 쓰지 않는다. 자막은 무료이고 1초면 끝난다.

Whisper 음성 인식과 비교해 고른 이유(2026-09-15 측정, docs/결정 기록은 CLAUDE.md):
  · 서버가 영상을 내려받지 않는다. 유튜브 링크를 그대로 넘기므로 유튜브 IP 차단과 무관하다
  · 소리뿐 아니라 화면 글자도 읽는다. 목소리 없이 글자만 나오는 AI 생성 건강 영상이 많다
  · 1분 쇼츠 6.3초. Whisper small은 60초 음성에 26초가 걸렸다
  · 자동 생성 자막이 "사포인", "한 겸역과"로 틀린 곳을 "사포닌", "항염 효과"로 옮겼다

이 모델은 ② 내용 추출에만 쓴다. 판정은 규칙표가 한다. 모델이 옮긴 글은 다른
본문과 똑같이 ③ 구조화를 거친다. 모델이 라벨을 정하지 않는다.

확인된 스펙(ai.google.dev/gemini-api/docs/video-understanding):
  POST https://generativelanguage.googleapis.com/v1beta/models/{모델}:generateContent
  헤더 x-goog-api-key, 본문 parts에 file_data.file_uri=유튜브 주소
  공개 영상만 된다. 무료 등급은 하루 8시간 분량까지. 미리보기 기능이다.
  무료 등급은 보낸 내용이 제품 개선에 쓰인다. 공개 영상 주소만 보낸다.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.domain.types import ExtractedContent

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# gemini-2.5-flash-lite는 새 사용자에게 막혀 있다(2026-09-15 실측 404).
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_TIMEOUT = 90.0

# 자동 생성 자막(0.65)보다 높고 사람이 올린 자막(0.9)보다 낮다. 측정에서 자동 자막보다
# 정확했지만, 모델이 받아 적는 방식이라 없는 말을 넣을 위험이 남아 있다.
GEMINI_CONFIDENCE = 0.75
# 429를 받으면 이 시간 동안 부르지 않는다. 한도가 찬 상태에서 계속 부르면 요청만 버린다.
RATE_LIMIT_COOLDOWN = 600.0
_KST = ZoneInfo("Asia/Seoul")

# 이보다 짧으면 옮긴 내용이 없다고 본다.
MIN_TEXT_LENGTH = 10

_NOTHING = "없음"
PROMPT = (
    "이 영상에서 들리는 말을 한국어 원문 그대로 받아 적어라. "
    "말로는 나오지 않고 화면에만 나오는 글자가 있으면 그 글자도 원문 그대로 적어라. "
    "같은 내용을 두 번 적지 마라. "
    "요약, 해설, 번역, 의견을 덧붙이지 말고, 확실히 들리거나 보이지 않는 내용은 적지 마라. "
    f"들리는 말과 화면 글자가 모두 없으면 '{_NOTHING}'만 적어라."
)


class GeminiVideoError(TranscriptFetchError):
    """다시 시도하면 될 수 있는 실패(한도 초과, 서버 오류, 통신 오류).

    TranscriptFetchError를 이어받아 파이프라인이 '잠시 뒤에 다시'로 답하고
    판정을 캐시하지 않게 한다. 제목만으로 얕은 판정이 굳는 것을 막는다.
    """


class GeminiVideoTranscriber:
    """AudioTranscriber 인터페이스를 만족한다. 영상 ID를 받아 본문을 돌려준다."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY가 없습니다. .env를 확인하세요.")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self.call_count = 0
        self._limited_until = 0.0
        self._usage: dict[str, int | str] = _fresh_usage()

    def usage(self) -> dict:
        """오늘(한국 시간) 사용량. 무료 등급 하루 한도를 넘기 전에 알아채려고 둔다."""
        self._roll_day()
        return {**self._usage, "limited_now": time.monotonic() < self._limited_until}

    def _roll_day(self) -> None:
        if self._usage["date"] != _today():
            self._usage = _fresh_usage()

    async def transcribe(self, video_id: str) -> ExtractedContent:
        body = {
            "contents": [
                {
                    "parts": [
                        {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={video_id}"}},
                        {"text": PROMPT},
                    ]
                }
            ],
            # 받아 적기다. 창의성이 필요 없고 같은 영상이면 같은 글이 나와야 한다.
            # 화면은 저해상도로 본다. 글자를 읽기에 충분했고 토큰이 3분의 1이다.
            "generationConfig": {"temperature": 0, "mediaResolution": "MEDIA_RESOLUTION_LOW"},
        }

        self._roll_day()
        if time.monotonic() < self._limited_until:
            raise GeminiVideoError("Gemini 한도 초과로 호출을 잠시 멈춘 상태다")

        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        self.call_count += 1
        self._usage["calls"] += 1

        started = time.perf_counter()
        try:
            response = await self._client.post(
                GEMINI_URL.format(model=self._model),
                headers={"x-goog-api-key": self._api_key},
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise GeminiVideoError(f"Gemini 통신 오류: {type(error).__name__}") from error
        elapsed = time.perf_counter() - started

        if response.status_code == 429:
            self._limited_until = time.monotonic() + RATE_LIMIT_COOLDOWN
            self._usage["rate_limited"] += 1
            logger.warning(
                "Gemini 한도 초과. %.0f초간 호출을 멈춘다. 오늘 호출 %s건: %s",
                RATE_LIMIT_COOLDOWN,
                self._usage["calls"],
                _error_message(response),
            )
            raise GeminiVideoError(f"Gemini 응답 429: {_error_message(response)}")
        if response.status_code >= 500:
            raise GeminiVideoError(f"Gemini 응답 {response.status_code}: {_error_message(response)}")
        if response.status_code != 200:
            # 비공개 영상, 지원하지 않는 영상 같은 경우다. 다시 해도 같으므로 내용 없음으로 본다.
            logger.warning(
                "Gemini가 영상을 읽지 못했다 video=%s status=%d: %s",
                video_id,
                response.status_code,
                _error_message(response),
            )
            return _empty()

        try:
            data = response.json()
        except ValueError:
            raise GeminiVideoError("Gemini 응답 형식이 예상과 다르다") from None

        text = _text_of(data)
        usage = data.get("usageMetadata") or {}
        self._usage["input_tokens"] += int(usage.get("promptTokenCount") or 0)
        self._usage["output_tokens"] += int(usage.get("candidatesTokenCount") or 0)
        logger.info(
            "Gemini 영상 전사 video=%s %.1f초 입력토큰=%s 출력토큰=%s 글자수=%d",
            video_id,
            elapsed,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            len(text),
        )

        if len(text) < MIN_TEXT_LENGTH or text.strip(" .'\"") == _NOTHING:
            return _empty()

        return ExtractedContent(
            text=text,
            has_transcript=True,
            confidence=GEMINI_CONFIDENCE,
            language="ko",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
            source="gemini",
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _today() -> str:
    return datetime.now(_KST).date().isoformat()


def _fresh_usage() -> dict[str, int | str]:
    return {"date": _today(), "calls": 0, "input_tokens": 0, "output_tokens": 0, "rate_limited": 0}


def _text_of(data: dict) -> str:
    """후보 응답에서 글만 모은다. 생각 과정(thought) 조각은 뺀다."""
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict) and not part.get("thought")
    ).strip()


def _error_message(response: httpx.Response) -> str:
    try:
        return str((response.json().get("error") or {}).get("message") or "")[:200]
    except ValueError:
        return ""


def _empty() -> ExtractedContent:
    return ExtractedContent(text="", has_transcript=False, confidence=0.0, source="gemini")
