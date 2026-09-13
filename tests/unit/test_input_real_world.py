"""① 입력 수신 — 실제로 들어올 입력 전수 검증.

처음 점검에서 32가지 중 15가지가 틀렸다. 그 32가지를 그대로 테스트로 남긴다.
완제품 기준이므로 한 가지라도 틀리면 1단계는 완성이 아니다.
"""

from __future__ import annotations

import pytest

from healthcheck.pipeline.step1_input import (
    MAX_INPUT_CHARS,
    cache_key,
    canonical_url,
    classify,
    extract_urls,
)

V = "dQw4w9WgXcQ"

YOUTUBE_VIDEO = [
    ("표준", f"https://www.youtube.com/watch?v={V}"),
    ("짧은 주소", f"https://youtu.be/{V}"),
    ("공유 파라미터", f"https://youtu.be/{V}?si=AbC_123"),
    ("모바일", f"https://m.youtube.com/watch?v={V}"),
    ("쇼츠", f"https://youtube.com/shorts/{V}"),
    ("라이브", f"https://www.youtube.com/live/{V}"),
    ("임베드", f"https://www.youtube.com/embed/{V}"),
    ("뮤직", f"https://music.youtube.com/watch?v={V}"),
    ("/v/ 형태", f"https://www.youtube.com/v/{V}"),
    ("nocookie", f"https://www.youtube-nocookie.com/embed/{V}"),
    ("app 파라미터 앞", f"https://www.youtube.com/watch?app=desktop&v={V}"),
    ("재생목록 포함", f"https://www.youtube.com/watch?v={V}&list=PLxyz"),
    ("제목과 줄바꿈", f"홍삼의 놀라운 효능\nhttps://youtu.be/{V}?si=x"),
    ("스킴 없음", f"youtube.com/watch?v={V}"),
    ("www만", f"www.youtube.com/watch?v={V}"),
    ("ID 뒤 한글", f"https://youtu.be/{V}이거봐"),
    ("앞에 인사말", f"좋은 아침이야 이거 봐라 https://youtu.be/{V} 몸에 좋다더라"),
]


@pytest.mark.parametrize("name,text", YOUTUBE_VIDEO, ids=[n for n, _ in YOUTUBE_VIDEO])
def test_유튜브_영상_링크(name, text):
    share = classify(text)
    assert share.kind == "YOUTUBE", name
    assert share.video_id == V, name


@pytest.mark.parametrize(
    "name,text",
    [
        ("11자를 넘는 ID", f"https://youtu.be/{V}X"),
        ("채널", "https://www.youtube.com/@healthchannel"),
        ("재생목록만", "https://www.youtube.com/playlist?list=PLxyz"),
        ("검색 결과", "https://www.youtube.com/results?search_query=홍삼"),
        ("watch인데 v 없음", "https://www.youtube.com/watch?list=PLxyz"),
    ],
)
def test_영상이_아닌_유튜브_링크(name, text):
    """기사로 처리해 헛조회하지 않는다. 엉뚱한 영상 ID를 만들지도 않는다."""
    share = classify(text)
    assert share.kind == "YOUTUBE_OTHER", name
    assert share.video_id is None, name


ARTICLES = [
    ("네이버 블로그", "https://blog.naver.com/anarche69/224382558879", "https://blog.naver.com/anarche69/224382558879"),
    ("괄호 안", "(https://blog.naver.com/anarche69/224382558879)", "https://blog.naver.com/anarche69/224382558879"),
    ("한글이 붙음", "https://blog.naver.com/anarche69/224382558879입니다", "https://blog.naver.com/anarche69/224382558879"),
    ("마침표로 끝남", "여기 보세요 https://uibaki.tistory.com/41.", "https://uibaki.tistory.com/41"),
    ("네이버 뉴스", "https://n.news.naver.com/article/001/0012345678", "https://n.news.naver.com/article/001/0012345678"),
    ("티스토리", "https://uibaki.tistory.com/41", "https://uibaki.tistory.com/41"),
    ("추적 파라미터", "https://uibaki.tistory.com/41?utm_source=kakao&utm_medium=share", "https://uibaki.tistory.com/41?utm_source=kakao&utm_medium=share"),
    ("스킴 없는 기사", "uibaki.tistory.com/41", "https://uibaki.tistory.com/41"),
    ("한글 경로는 유지", "https://ko.wikipedia.org/wiki/홍삼", "https://ko.wikipedia.org/wiki/홍삼"),
]


@pytest.mark.parametrize("name,text,expected", ARTICLES, ids=[n for n, _, _ in ARTICLES])
def test_기사와_블로그_링크(name, text, expected):
    share = classify(text)
    assert share.kind == "OTHER_LINK", name
    assert share.url == expected, name


@pytest.mark.parametrize("url", ["https://naver.me/xAbCdEf", "https://bit.ly/3abcdef", "https://kko.to/abcdef"])
def test_단축_링크는_풀어야_한다고_표시한다(url):
    """풀기 전에는 실제 대상이 영상인지 글인지 알 수 없다."""
    share = classify(url)
    assert share.kind == "OTHER_LINK"
    assert share.needs_resolution is True


def test_일반_링크는_풀_필요가_없다():
    assert classify("https://uibaki.tistory.com/41").needs_resolution is False


def test_텍스트만():
    share = classify("홍삼이 면역력에 좋다고 들었어요")
    assert share.kind == "PLAIN_TEXT"
    assert share.url is None


def test_빈_입력():
    share = classify("")
    assert share.kind == "PLAIN_TEXT"
    assert share.raw_text == ""


def test_아주_긴_입력은_자른다():
    share = classify("홍삼 " * 5000)
    assert len(share.raw_text) == MAX_INPUT_CHARS
    assert share.truncated is True


def test_긴_글_뒤쪽의_링크도_찾는다():
    """링크 추출은 자르기 전에 한다."""
    share = classify("홍삼 " * 2000 + f" https://youtu.be/{V}")
    assert share.kind == "YOUTUBE"
    assert share.truncated is True


# ── 여러 링크, 오탐 방지 ─────────────────────────────────────


def test_영상과_채널이_함께_있으면_영상을_고른다():
    share = classify(f"https://www.youtube.com/@ch https://youtu.be/{V}")
    assert share.kind == "YOUTUBE"


def test_채널과_기사가_함께_있으면_기사를_고른다():
    share = classify("https://www.youtube.com/@ch https://uibaki.tistory.com/41")
    assert share.kind == "OTHER_LINK"


def test_이메일_주소를_링크로_보지_않는다():
    assert classify("문의는 health@naver.com 으로 주세요").kind == "PLAIN_TEXT"


def test_숫자와_단위를_링크로_보지_않는다():
    assert classify("홍삼 1.5g을 하루 2회 드세요").kind == "PLAIN_TEXT"


def test_경로에_youtube_문자열이_있어도_속지_않는다():
    share = classify(f"https://evil.example.com/youtube.com/watch?v={V}")
    assert share.kind == "OTHER_LINK"


def test_링크_순서를_지킨다():
    urls = extract_urls("첫째 https://a.example.com/1 둘째 b.example.com/2")
    assert urls == ["https://a.example.com/1", "https://b.example.com/2"]


# ── 캐시 키 ──────────────────────────────────────────────────


def test_같은_영상은_주소_형태가_달라도_같은_키():
    keys = {cache_key(classify(text)) for _, text in YOUTUBE_VIDEO}
    assert keys == {f"yt:{V}"}


def test_추적_파라미터가_달라도_같은_키():
    a = cache_key(classify("https://uibaki.tistory.com/41"))
    b = cache_key(classify("https://uibaki.tistory.com/41?utm_source=kakao&fbclid=x"))
    assert a == b


def test_앞뒤_인사말이_달라도_같은_키():
    a = cache_key(classify("https://uibaki.tistory.com/41"))
    b = cache_key(classify("엄마 이거 봐 https://uibaki.tistory.com/41 좋대"))
    assert a == b


def test_네이버_블로그_모바일과_PC_주소는_같은_키():
    keys = {
        cache_key(classify(url))
        for url in (
            "https://blog.naver.com/anarche69/224382558879",
            "https://m.blog.naver.com/anarche69/224382558879",
            "https://blog.naver.com/PostView.naver?blogId=anarche69&logNo=224382558879",
        )
    }
    assert len(keys) == 1


def test_의미있는_파라미터는_키에_남긴다():
    """추적 파라미터만 지운다. 글을 구분하는 파라미터를 지우면 다른 글이 섞인다."""
    a = cache_key(classify("https://news.example.com/view?id=1"))
    b = cache_key(classify("https://news.example.com/view?id=2"))
    assert a != b


def test_텍스트는_공백_차이를_무시한다():
    a = cache_key(classify("홍삼이  면역력에\n좋다"))
    b = cache_key(classify("홍삼이 면역력에 좋다"))
    assert a == b


def test_정규화_주소_형태():
    assert canonical_url("HTTPS://WWW.Example.com/a/?b=2&a=1&utm_x=9#frag") == "https://example.com/a?a=1&b=2"
