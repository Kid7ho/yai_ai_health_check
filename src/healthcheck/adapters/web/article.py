"""기사·블로그 본문 추출 실제 구현.

trafilatura로 본문만 골라낸다. 광고와 댓글, 관련기사 목록이 섞이면 엉뚱한
성분과 효능이 잡힌다.

안전 장치 셋을 넣었다.

1. 주소 검사
   사용자가 아무 주소나 보낼 수 있다. 사내망이나 로컬호스트로 향하는 요청을
   막는다. 서버가 자기 자신이나 내부 서비스를 긁는 통로가 되면 안 된다.

2. 크기 제한
   본문이 아무리 길어도 정해진 양까지만 읽는다. 큰 파일을 보내 메모리를
   말리는 요청을 막는다.

3. 가져온 내용은 데이터로만 다룬다
   페이지에 어떤 문장이 있어도 그것이 판정 규칙을 바꾸지 못한다. 규칙 엔진은
   성분과 효능 표현만 본다.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx
import lxml.etree
import lxml.html
import trafilatura

from healthcheck.adapters.web.safe_http import (
    FetchError,
    UnsafeUrlError,
    assert_safe_url,
    safe_request,
)
from healthcheck.domain.types import ExtractedContent

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
# 본문으로 읽어 들일 최대 바이트. 한국어 기사 한 편은 보통 수십 KB다.
MAX_BYTES = 2_000_000
# 제목을 뺀 본문이 이보다 짧으면 기사로 보지 않는다. 80자였을 때 종료된
# 서비스의 136자짜리 안내문이 본문으로 통과했다.
MIN_TEXT_LENGTH = 200

# 짧은 페이지에 이런 문구가 있으면 기사가 아니라 안내·오류 페이지로 본다.
# 긴 기사가 "서비스 종료"를 언급하는 경우까지 거르지 않도록 짧은 페이지에만 쓴다.
NOTICE_PAGE_MAX_LENGTH = 800
NOTICE_PAGE_MARKERS = (
    "서비스가 종료",
    "서비스를 종료",
    "운영팀입니다",
    "로그인이 필요",
    "로그인 후 이용",
    "페이지를 찾을 수 없",
    "존재하지 않는 페이지",
    "삭제된 게시물",
    "삭제되었거나",
    "비공개 글",
    "접근 권한이 없",
)



# 네이버 블로그는 본문을 iframe 안에서 따로 불러온다. 겉 페이지를 긁으면
# 빈 껍데기만 나온다. 실제로 확인했을 때 0자였다. 모바일 주소는 본문을 바로
# 담고 있어서 그쪽으로 바꿔 요청한다. PostView 주소도 본문이 나오지만
# "저작권 침해가 우려되는 컨텐츠" 같은 안내문이 앞에 붙는다.
_NAVER_POST_RE = re.compile(
    r"^(?:m\.)?blog\.naver\.com/(?P<blog>[A-Za-z0-9_\-]+)/(?P<log>\d+)"
)
_NAVER_HOSTS = ("blog.naver.com", "m.blog.naver.com")

# 본문에 섞여 나오는 안내문. 주장이 아니므로 버린다.
_BOILERPLATE = (
    "저작권 침해가 우려되는",
    "이 글은 네이버 블로그에서",
)


def normalize_url(url: str) -> str:
    """사이트별로 본문이 실제로 담긴 주소로 바꾼다.

    지금은 네이버 블로그만 다룬다. 어르신이 받는 링크에서 비중이 크고,
    겉 페이지를 그대로 긁으면 본문이 한 글자도 나오지 않기 때문이다.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host not in _NAVER_HOSTS:
        return url

    target = f"{host}{parsed.path}"
    match = _NAVER_POST_RE.match(target)
    if match:
        return f"https://m.blog.naver.com/{match.group('blog')}/{match.group('log')}"

    # blog.naver.com/PostView.naver?blogId=...&logNo=... 형태
    query = parse_qs(parsed.query)
    blog = (query.get("blogId") or [""])[0]
    log = (query.get("logNo") or [""])[0]
    if blog and log.isdigit():
        return f"https://m.blog.naver.com/{blog}/{log}"

    return url


# 기존 이름을 유지한다. 호출하는 쪽과 테스트가 이 이름을 쓴다.
ArticleFetchError = FetchError
UnsafeUrlError = UnsafeUrlError
assert_safe_url = assert_safe_url


def _naver_smart_editor_text(raw: bytes) -> str:
    """네이버 블로그 스마트에디터 본문을 문단 단위로 뽑는다.

    스마트에디터 글은 한 줄마다 짧은 문단 요소로 나뉜다. trafilatura는 이런
    짧은 조각을 본문이 아닌 블록으로 보고 버렸다. 실채널에서 공유된 글이
    본문 1,707자인데 제목과 페이지 메타데이터 JSON만 남았다.
    글 모듈 하나를 한 줄로 이어 붙인다. 같은 모듈의 줄바꿈은 문장 중간이다.
    """
    try:
        tree = lxml.html.fromstring(raw)
    except (ValueError, lxml.etree.ParserError):
        return ""

    lines: list[str] = []
    for module in tree.xpath(
        '//*[contains(@class, "se-main-container")]'
        '//*[contains(concat(" ", normalize-space(@class), " "), " se-module-text ")]'
    ):
        parts = [
            # 빈 문단에 폭 없는 공백(U+200B)을 넣어 두므로 함께 지운다.
            " ".join(p.text_content().replace("\u200b", " ").split())
            for p in module.xpath('.//*[contains(@class, "se-text-paragraph")]')
        ]
        line = " ".join(part for part in parts if part)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _as_dict(document) -> dict:
    """trafilatura 버전에 따라 결과가 dict이거나 Document 객체다."""
    if document is None:
        return {}
    if isinstance(document, dict):
        return document
    if hasattr(document, "as_dict"):
        return document.as_dict()
    return vars(document)


def _drop_boilerplate(text: str) -> str:
    """사이트 안내문을 버린다. 주장으로 읽히면 엉뚱한 판정이 나온다."""
    lines = [
        line
        for line in text.splitlines()
        if not any(marker in line for marker in _BOILERPLATE)
    ]
    return "\n".join(lines).strip()


class TrafilaturaArticleExtractor:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        client: httpx.AsyncClient | None = None,
        check_url: bool = True,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._client = client
        self._owns_client = client is None
        self._check_url = check_url
        self.call_count = 0

    async def fetch(self, url: str) -> ExtractedContent:
        target = normalize_url(url)
        if target != url:
            logger.info("본문 주소로 바꿔 요청한다: %s", target)

        self.call_count += 1
        raw = await self._download(target)

        # 원본 바이트를 그대로 넘긴다. 직접 문자열로 풀면 응답 헤더에 문자셋이
        # 없는 EUC-KR 페이지가 깨졌다. trafilatura는 meta charset을 보고 푼다.
        document = trafilatura.bare_extraction(
            raw,
            url=target,
            with_metadata=True,
            include_comments=False,  # 댓글에는 엉뚱한 성분 이야기가 섞인다
            include_tables=False,
            favor_precision=True,  # 본문이 아닌 블록을 버리는 쪽을 택한다
        )
        fields = _as_dict(document)

        body = _drop_boilerplate(fields.get("text") or "")
        title = str(fields.get("title") or "").strip()

        if (urlparse(target).hostname or "").lower() in _NAVER_HOSTS:
            editor_body = _drop_boilerplate(_naver_smart_editor_text(raw))
            if editor_body:
                body = editor_body

        if len(body) < MIN_TEXT_LENGTH:
            logger.info("본문이 너무 짧아 기사로 보지 않는다 url=%s 길이=%d", url, len(body))
            return ExtractedContent(text="", has_transcript=False, confidence=0.0, source="article")

        if len(body) <= NOTICE_PAGE_MAX_LENGTH and any(m in body for m in NOTICE_PAGE_MARKERS):
            logger.info("안내·오류 페이지로 보여 기사로 보지 않는다 url=%s", url)
            return ExtractedContent(text="", has_transcript=False, confidence=0.0, source="article")

        # 헤드라인을 본문 앞에 붙인다. 건강 오정보는 핵심 주장이 제목에 담기는
        # 경우가 많은데, 사이트에 따라 추출 본문에 제목이 빠졌다.
        text = body if not title or body.startswith(title) else f"{title}\n{body}"

        logger.info("기사 본문 확보 url=%s 제목=%s 글자수=%d", url, bool(title), len(text))
        return ExtractedContent(
            text=text,
            has_transcript=True,
            # 기사는 사람이 쓴 글이라 자동 생성 자막보다 신뢰도가 높다.
            # 다만 본문 추출이 광고를 섞을 수 있어 수동 자막과 같게 두지는 않는다.
            confidence=0.85,
            language="ko",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
            source="article",
        )

    async def _download(self, url: str) -> bytes:
        """리다이렉트마다 주소를 검사하고, 받는 도중에 크기를 제한한다."""
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True

        response, _ = await safe_request(
            self._client,
            "GET",
            url,
            timeout=self._timeout,
            check_url=self._check_url,
            max_bytes=self._max_bytes,
        )

        if response.status_code != 200:
            # 5xx와 429는 사이트 쪽 일시 장애나 호출 제한이다. 다시 시도하면 될 수 있다.
            retryable = response.status_code >= 500 or response.status_code == 429
            raise ArticleFetchError(
                f"기사 조회 응답 {response.status_code}", retryable=retryable
            )

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise ArticleFetchError(f"본문을 읽을 수 없는 형식입니다: {content_type}")

        return response.content

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
