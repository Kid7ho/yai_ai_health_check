"""① 입력 수신 단위 테스트."""

from __future__ import annotations

import pytest

from healthcheck.pipeline.step1_input import cache_key, classify, extract_video_id

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        # 카카오톡 공유에 붙는 파라미터가 앞에 오는 경우
        f"https://www.youtube.com/watch?feature=share&v={VIDEO_ID}",
        # 뒤에 파라미터가 붙는 경우
        f"https://youtu.be/{VIDEO_ID}?si=AbCdEf",
    ],
)
def test_유튜브_주소_형태가_달라도_같은_영상_ID를_뽑는다(url):
    assert extract_video_id(url) == VIDEO_ID
    assert classify(url).kind == "YOUTUBE"


def test_같은_영상이면_주소_형태가_달라도_캐시_키가_같다():
    """캐시 적중률이 Liner 토큰 비용과 응답 지연을 함께 줄인다."""
    a = classify(f"https://youtu.be/{VIDEO_ID}")
    b = classify(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s")
    assert cache_key(a) == cache_key(b) == f"yt:{VIDEO_ID}"


def test_링크_앞뒤에_인사말이_붙어도_찾아낸다():
    """어르신은 링크만 보내지 않는다. 인사말과 함께 보낸다."""
    share = classify(f"좋은 아침이야 이거 봐라 https://youtu.be/{VIDEO_ID} 몸에 좋다더라")
    assert share.kind == "YOUTUBE"
    assert share.video_id == VIDEO_ID


def test_유튜브가_아닌_링크는_따로_분류한다():
    share = classify("https://blog.example.com/health/123")
    assert share.kind == "OTHER_LINK"
    assert share.video_id is None
    assert share.url == "https://blog.example.com/health/123"


def test_유튜브_링크가_섞여_있으면_유튜브를_고른다():
    share = classify(f"https://blog.example.com/a https://youtu.be/{VIDEO_ID}")
    assert share.kind == "YOUTUBE"
    assert share.video_id == VIDEO_ID


def test_링크가_없으면_텍스트로_본다():
    share = classify("냉동 블루베리가 당뇨를 완치시킨다")
    assert share.kind == "PLAIN_TEXT"
    assert share.video_id is None


def test_빈_입력도_예외를_던지지_않는다():
    """어르신이 사진만 보내는 경우 발화가 비어 올 수 있다."""
    share = classify("")
    assert share.kind == "PLAIN_TEXT"
    assert share.raw_text == ""


def test_유튜브_채널_주소는_영상이_아니다():
    assert extract_video_id("https://www.youtube.com/@somechannel") is None


def test_다른_사이트에_youtube_문자열이_있어도_속지_않는다():
    """호스트를 확인한다. 경로에 youtube.com이 들어간 주소에 속으면 안 된다."""
    assert extract_video_id("https://evil.example.com/youtube.com/watch?v=dQw4w9WgXcQ") is None
