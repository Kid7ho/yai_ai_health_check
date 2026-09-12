"""① 입력 수신 — 링크 종류 판별.

어르신이 채널로 보낸 원문 한 줄에서 유튜브 링크를 찾아낸다.
외부 호출이 없는 순수 함수다. 단위 테스트만으로 검증한다.

카카오톡으로 공유된 유튜브 링크는 형태가 일정하지 않다.
짧은 주소(youtu.be), 공유 파라미터가 붙은 주소, 쇼츠, 모바일 주소가 섞여 온다.
같은 영상이 다른 주소로 와도 같은 캐시 키가 나와야 하므로 영상 ID만 뽑아 쓴다.
"""

from __future__ import annotations

import re

from healthcheck.domain.types import InputKind, ShareInput

# 원문에서 URL을 찾는다. 카카오톡은 링크 앞뒤에 텍스트를 함께 보낼 수 있다.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# 유튜브 영상 ID는 11자다.
_VIDEO_ID = r"(?P<vid>[0-9A-Za-z_-]{11})"

# 유튜브 주소 형태들. 위에서 아래 순서로 맞춰 본다.
_YOUTUBE_PATTERNS = (
    re.compile(rf"youtu\.be/{_VIDEO_ID}"),
    re.compile(rf"youtube\.com/watch\?(?:[^\s]*&)?v={_VIDEO_ID}"),
    re.compile(rf"youtube\.com/shorts/{_VIDEO_ID}"),
    re.compile(rf"youtube\.com/live/{_VIDEO_ID}"),
    re.compile(rf"youtube\.com/embed/{_VIDEO_ID}"),
)

_YOUTUBE_HOST_RE = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be)", re.IGNORECASE)


def classify(raw_text: str) -> ShareInput:
    """원문을 받아 링크 종류를 판별한다.

    링크가 여러 개면 첫 번째 유튜브 링크를 쓴다. 유튜브가 없으면 첫 번째 링크를 쓴다.
    링크가 없으면 텍스트로 본다.
    """
    text = (raw_text or "").strip()
    urls = _URL_RE.findall(text)

    for url in urls:
        video_id = extract_video_id(url)
        if video_id:
            return ShareInput(raw_text=text, kind="YOUTUBE", video_id=video_id, url=url)

    if urls:
        # 유튜브가 아닌 링크다. 이번 MVP는 내용을 추출하지 않는다.
        # 규칙 R-010이 자막 없음으로 받아 UNKNOWN을 돌려준다.
        return ShareInput(raw_text=text, kind="OTHER_LINK", video_id=None, url=urls[0])

    return ShareInput(raw_text=text, kind="PLAIN_TEXT", video_id=None, url=None)


def extract_video_id(url: str) -> str | None:
    """유튜브 주소에서 영상 ID를 뽑는다. 유튜브가 아니면 None."""
    if not _YOUTUBE_HOST_RE.search(_host_of(url)):
        return None
    for pattern in _YOUTUBE_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group("vid")
    return None


def _host_of(url: str) -> str:
    """정규식으로 호스트 부분만 떼어 낸다.

    urlparse를 쓰지 않는 이유는 어르신이 보낸 원문에 프로토콜이 빠진 주소나
    한글이 섞인 주소가 오는 경우가 있어서다. 여기서는 호스트 판별만 하면 된다.
    """
    stripped = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    return stripped.split("/", 1)[0].split("?", 1)[0]


def cache_key(share_input: ShareInput) -> str:
    """캐시 1단(판정 캐시)의 키.

    같은 영상이면 주소 형태가 달라도 같은 키가 나와야 한다. 경로당과 복지관에서
    같은 영상이 여러 어르신에게 돌기 때문에 이 키의 안정성이 적중률을 좌우한다.
    """
    import hashlib

    if share_input.kind == "YOUTUBE" and share_input.video_id:
        return f"yt:{share_input.video_id}"
    digest = hashlib.sha256(share_input.raw_text.encode("utf-8")).hexdigest()[:32]
    return f"text:{digest}"


__all__ = ["classify", "extract_video_id", "cache_key", "InputKind"]
