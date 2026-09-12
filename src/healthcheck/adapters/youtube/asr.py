"""음성 인식으로 자막을 대신한다 (yt-dlp + faster-whisper).

자막이 없는 영상을 처리하기 위한 대체 경로다. 자막이 있으면 쓰지 않는다.

알아 두어야 할 대가가 셋이다.

1. 느리다
   자막 추출은 1초 안에 끝나지만 음성 인식은 분 단위다. CPU에서 5분짜리
   영상이 수십 초에서 몇 분 걸린다. 발표 자료의 "약 15초" 안내와 맞지 않는다.
   그래서 기본값으로 앞부분만 처리하고, 전체 처리는 설정으로 켠다.

2. 부정확하다
   자동 생성 자막과 비슷하거나 조금 나은 수준이다. 성분명과 효능 표현을
   잘못 옮기면 ③ 구조화가 엉뚱한 주장을 만든다. 그래서 신뢰도를 자막보다
   낮게 주고, 모델이 스스로 낮게 평가한 구간이 많으면 판정을 보류시킨다.

3. 모델을 내려받는다
   첫 실행에서 모델 파일을 받는다. small 기준 수백 MB다. 컨테이너에
   미리 넣어 두지 않으면 첫 요청이 크게 늦어진다.

기본값이 꺼짐인 이유는 위 셋 때문이다. ASR_ENABLED=1로 켠다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path

from healthcheck.domain.types import ExtractedContent

logger = logging.getLogger(__name__)

# 모델이 구간을 스스로 얼마나 확신했는지. 이보다 낮으면 신뢰도를 깎는다.
# faster-whisper의 avg_logprob는 0에 가까울수록 확신이 높고 음수로 갈수록 낮다.
_LOGPROB_FLOOR = -1.0

# 음성 인식 결과의 기본 신뢰도. 자막_신뢰도_최저(0.6)를 겨우 넘긴다.
# 자동 생성 자막(0.65)보다 낮게 두어, 자막이 있으면 자막을 쓰게 한다.
ASR_CONFIDENCE = 0.62
# 모델이 확신하지 못한 구간이 많을 때의 신뢰도. 규칙 R-010이 판정을 보류한다.
ASR_LOW_CONFIDENCE = 0.4


class AsrError(RuntimeError):
    """음성 인식 실패. ② 단계가 자막 없음과 같게 취급한다."""


class WhisperAsrTranscriber:
    def __init__(
        self,
        *,
        model_size: str = "small",
        max_seconds: int = 300,
        language: str = "ko",
        proxy_url: str | None = None,
        compute_type: str = "int8",
    ) -> None:
        self._model_size = model_size
        self._max_seconds = max_seconds
        self._language = language
        self._proxy_url = proxy_url
        self._compute_type = compute_type
        self._model = None
        self.call_count = 0

    async def transcribe(self, video_id: str) -> ExtractedContent:
        self.call_count += 1
        # 내려받기와 추론 모두 동기·CPU 작업이다. 이벤트 루프를 막으면
        # 5초 SLA를 지켜야 하는 다른 요청까지 함께 멈춘다.
        return await asyncio.to_thread(self._run, video_id)

    # ── 동기 구간 ────────────────────────────────────────────

    def _run(self, video_id: str) -> ExtractedContent:
        with tempfile.TemporaryDirectory(prefix="healthcheck-asr-") as workdir:
            audio_path = self._download_audio(video_id, Path(workdir))
            return self._transcribe_file(audio_path)

    def _download_audio(self, video_id: str, workdir: Path) -> Path:
        """음성만 내려받는다. 화면은 필요 없으므로 받지 않는다."""
        import yt_dlp
        from yt_dlp.utils import download_range_func

        options: dict = {
            "format": "bestaudio/best",
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            # 재생목록 링크가 와도 영상 하나만 받는다.
            "noplaylist": True,
        }
        if self._proxy_url:
            options["proxy"] = self._proxy_url
        if self._max_seconds > 0:
            # 앞부분만 받는다. 주장은 보통 도입부에서 반복되고, 전체를 받으면
            # 내려받기와 추론 시간이 영상 길이에 비례해 늘어난다.
            options["download_ranges"] = download_range_func(None, [(0, self._max_seconds)])
            options["force_keyframes_at_cuts"] = False

        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except Exception as error:
            raise AsrError(f"음성 내려받기 실패: {str(error).splitlines()[0][:160]}") from error

        files = sorted(workdir.glob("*"))
        if not files:
            raise AsrError("음성 파일을 받지 못했다.")
        return files[0]

    def _transcribe_file(self, path: Path) -> ExtractedContent:
        model = self._ensure_model()
        try:
            segments, info = model.transcribe(
                str(path),
                language=self._language,
                # 무음 구간을 건너뛴다. 배경음만 있는 구간에서 환각이 생긴다.
                vad_filter=True,
                beam_size=1,
            )
            parts: list[str] = []
            logprobs: list[float] = []
            for segment in segments:
                parts.append(segment.text.strip())
                logprobs.append(segment.avg_logprob)
        except Exception as error:
            raise AsrError(f"음성 인식 실패: {error}") from error

        text = " ".join(part for part in parts if part).strip()
        if not text:
            logger.info("음성 인식 결과가 비었다.")
            return ExtractedContent(text="", has_transcript=False, confidence=0.0)

        average = sum(logprobs) / len(logprobs) if logprobs else _LOGPROB_FLOOR
        confidence = ASR_CONFIDENCE if average >= _LOGPROB_FLOOR else ASR_LOW_CONFIDENCE

        logger.info(
            "음성 인식 완료 글자수=%d 구간=%d 평균확신도=%.2f 신뢰도=%.2f",
            len(text),
            len(parts),
            average,
            confidence,
        )

        return ExtractedContent(
            text=text,
            has_transcript=True,
            confidence=confidence,
            language=getattr(info, "language", self._language),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        )

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info("음성 인식 모델 준비: %s (%s)", self._model_size, self._compute_type)
            self._model = WhisperModel(
                self._model_size, device="cpu", compute_type=self._compute_type
            )
        return self._model
