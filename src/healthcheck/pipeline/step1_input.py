"""① 입력 수신 — 링크 추출과 종류 판별.

어르신이 채널로 보낸 원문에서 링크를 찾고 종류를 정한다. 외부 호출이 없는
순수 함수다. 단축 링크를 푸는 일만은 네트워크가 필요해서 오케스트레이터가
따로 맡는다. 여기서는 "풀어야 한다"는 표시만 남긴다.

실제 입력 32가지로 검증했을 때 틀렸던 것들을 고친 구현이다.

  · 주소 앞 https://가 없으면 링크로 못 알아봤다
  · 주소 끝에 붙은 괄호나 한글까지 주소로 붙였다
  · youtube.com/v/ID, youtube-nocookie.com 형태를 몰랐다
  · 영상 ID가 11자를 넘어도 앞 11자로 잘라 엉뚱한 영상을 찾았다
  · 채널·재생목록 링크를 기사로 처리해 헛조회했다
  · 같은 글인데 추적 파라미터나 인사말 때문에 캐시 키가 달랐다
  · 입력 길이에 상한이 없었다
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from healthcheck.domain.types import InputKind, ShareInput

# 이 길이를 넘는 입력은 자른다. 카카오 말풍선으로 붙여넣는 건강정보 문장은
# 길어야 몇백 자다. 이보다 긴 입력은 글을 통째로 붙여넣은 경우이고, 주장
# 추출 비용과 로그 크기만 키운다. 링크 추출은 자르기 전에 한다.
MAX_INPUT_CHARS = 3000

_VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
    }
)

# 경로의 첫 조각 뒤에 영상 ID가 오는 형태
_YOUTUBE_ID_PREFIXES = frozenset({"shorts", "live", "embed", "v", "e"})

# 풀어야 실제 주소를 알 수 있는 단축 링크 도메인. 카카오톡으로 공유되는
# 링크에서 흔한 것들이다. 네이버 블로그를 공유하면 naver.me가 붙는다.
SHORT_LINK_HOSTS = frozenset(
    {
        "naver.me",
        "kko.to",
        "bit.ly",
        "han.gl",
        "url.kr",
        "me2.do",
        "t.co",
        "goo.gl",
        "tinyurl.com",
        "vo.la",
        "buly.kr",
        "lrl.kr",
        "zrr.kr",
        "c11.kr",
        "is.gd",
        "ow.ly",
        "cutt.ly",
        "rebrand.ly",
        "shorturl.at",
    }
)

# 로그인해야 글을 볼 수 있는 곳. 서버가 가져가면 로그인 화면만 받는다.
# 실측에서 네이버 카페와 인스타그램은 0자, 페이스북은 400이었다.
# 호스트 끝이 일치하면 해당한다(m.facebook.com 등 하위 도메인 포함).
LOGIN_WALL_SITES: dict[str, str] = {
    "cafe.naver.com": "네이버 카페",
    "instagram.com": "인스타그램",
    "facebook.com": "페이스북",
    "fb.com": "페이스북",
    "fb.watch": "페이스북",
    "x.com": "X(트위터)",
    "twitter.com": "X(트위터)",
    "tiktok.com": "틱톡",
    "band.us": "밴드",
    "story.kakao.com": "카카오스토리",
    "threads.net": "스레드",
    "threads.com": "스레드",
}

# 캐시 키를 만들 때 지우는 추적 파라미터. 같은 글인데 공유 경로마다 다른
# 값이 붙어 캐시가 맞지 않았다.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "si",
        "feature",
        "ref",
        "ref_src",
        "from",
        "share",
        "spm",
        "_ga",
        "mc_cid",
        "mc_eid",
        "isappinstalled",
    }
)

# 스킴 없이 적힌 주소를 찾는다. 이메일 주소의 도메인은 제외한다.
_BARE_URL_START = re.compile(
    r"(?<![@\w.])"
    r"(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|kr|io|me|ly|be|to|tv|app|co|info|news|blog|gl|at|do|gd)"
    r"(?=[/?#:]|\s|$|[^\x00-\x7f])",
    re.IGNORECASE,
)
_SCHEME_URL_START = re.compile(r"https?://", re.IGNORECASE)

# 주소가 아닌 것으로 보는 문자
_URL_STOP = set(' \t\r\n<>"\'`')
# 주소 끝에 붙어도 주소가 아닌 문장부호
_TRAILING_PUNCT = ".,;:!?"
_BRACKETS = {")": "(", "]": "[", "}": "{", "》": "《", "」": "「", "』": "『"}


# ─────────────────────────────────────────────────────────────
# 공개 함수
# ─────────────────────────────────────────────────────────────


def classify(raw_text: str) -> ShareInput:
    """원문을 받아 링크 종류를 판별한다.

    링크가 여러 개면 영상 링크, 기사·블로그 링크, 영상이 아닌 유튜브 링크
    순서로 고른다. 어르신이 영상과 함께 보낸 채널 링크보다 영상 자체가
    확인 대상이기 때문이다.
    """
    text = (raw_text or "").strip()

    # 링크 추출은 자르기 전에 한다. 긴 글 뒤쪽에 붙은 링크도 놓치지 않는다.
    urls = extract_urls(text)

    truncated = len(text) > MAX_INPUT_CHARS
    if truncated:
        text = text[:MAX_INPUT_CHARS]

    candidates = [_describe(url) for url in urls]

    for kind in ("YOUTUBE", "OTHER_LINK", "YOUTUBE_OTHER"):
        for url, found_kind, video_id in candidates:
            if found_kind == kind:
                return ShareInput(
                    raw_text=text,
                    kind=found_kind,
                    video_id=video_id,
                    url=url,
                    truncated=truncated,
                    needs_resolution=found_kind == "OTHER_LINK" and is_short_link(url),
                )

    return ShareInput(raw_text=text, kind="PLAIN_TEXT", truncated=truncated)


def extract_urls(text: str) -> list[str]:
    """원문에서 주소를 순서대로 뽑는다. 스킴이 없으면 https를 붙인다."""
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []

    for match in _SCHEME_URL_START.finditer(text):
        end, url = _take_url(text, match.start())
        if url:
            found.append((match.start(), url))
            taken.append((match.start(), end))

    for match in _BARE_URL_START.finditer(text):
        start = match.start()
        if any(a <= start < b for a, b in taken):
            continue
        end, url = _take_url(text, start)
        if url:
            found.append((start, "https://" + url))
            taken.append((start, end))

    return [url for _, url in sorted(found)]


def extract_video_id(url: str) -> str | None:
    """유튜브 주소에서 영상 ID를 뽑는다. 영상이 아니거나 유튜브가 아니면 None.

    ID는 정확히 11자여야 한다. 11자를 넘는 조각을 앞 11자로 자르면 엉뚱한
    영상을 조회하게 된다.
    """
    parsed = urlparse(_ensure_scheme(url))
    host = _bare_host(parsed.hostname)
    if host not in _YOUTUBE_HOSTS:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]

    if host == "youtu.be":
        candidate = segments[0] if segments else ""
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    if segments[:1] == ["watch"]:
        for key, value in parse_qsl(parsed.query):
            if key == "v" and _VIDEO_ID_RE.match(value):
                return value
        return None

    if len(segments) >= 2 and segments[0] in _YOUTUBE_ID_PREFIXES:
        return segments[1] if _VIDEO_ID_RE.match(segments[1]) else None

    return None


def is_youtube(url: str) -> bool:
    return _bare_host(urlparse(_ensure_scheme(url)).hostname) in _YOUTUBE_HOSTS


def is_short_link(url: str) -> bool:
    return _bare_host(urlparse(_ensure_scheme(url)).hostname) in SHORT_LINK_HOSTS


def login_wall_site(url: str | None) -> str | None:
    """로그인이 필요한 사이트면 어르신에게 보여 줄 이름을, 아니면 None을 돌려준다."""
    if not url:
        return None
    host = _bare_host(urlparse(_ensure_scheme(url)).hostname)
    for domain, name in LOGIN_WALL_SITES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def canonical_url(url: str) -> str:
    """같은 글을 가리키는 주소를 하나의 형태로 모은다. 캐시 키에 쓴다.

    호스트를 소문자로 하고, www를 떼고, 조각(#)과 추적 파라미터를 지우고,
    남은 파라미터를 정렬하고, 끝 슬래시를 뗀다. 네이버 블로그는 모바일·PC·
    PostView 주소를 하나로 모은다.
    """
    parsed = urlparse(_ensure_scheme(url))
    host = _bare_host(parsed.hostname)
    path = parsed.path.rstrip("/") or "/"

    naver = _naver_blog_post(host, parsed)
    if naver:
        return f"https://blog.naver.com/{naver[0]}/{naver[1]}"

    query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    )

    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    return urlunparse(("https", f"{host}{port}", path, "", urlencode(query), ""))


def cache_key(share_input: ShareInput) -> str:
    """캐시 1단(판정 캐시)의 키.

    같은 대상을 가리키면 같은 키가 나와야 한다. 경로당과 복지관에서 같은
    영상과 글이 여러 어르신에게 돌기 때문에 이 키의 안정성이 적중률을 좌우한다.
    링크가 있으면 링크만 보고, 앞뒤 인사말은 키에 넣지 않는다.
    """
    if share_input.kind == "YOUTUBE" and share_input.video_id:
        return f"yt:{share_input.video_id}"

    if share_input.url:
        return "url:" + _digest(canonical_url(share_input.url))

    normalized = re.sub(r"\s+", " ", share_input.raw_text).strip()
    return "text:" + _digest(normalized)


# ─────────────────────────────────────────────────────────────
# 내부 도구
# ─────────────────────────────────────────────────────────────


def _describe(url: str) -> tuple[str, InputKind, str | None]:
    video_id = extract_video_id(url)
    if video_id:
        return url, "YOUTUBE", video_id
    if is_youtube(url):
        return url, "YOUTUBE_OTHER", None
    return url, "OTHER_LINK", None


def _take_url(text: str, start: int) -> tuple[int, str]:
    """start부터 주소가 끝나는 곳까지 읽는다.

    한글이 붙어 나오는 경우를 구분한다. "…/224382558879입니다"처럼 영문으로
    시작한 경로 조각 뒤에 한글이 붙으면 거기서 끊는다. "…/wiki/홍삼"처럼
    조각이 한글로 시작하면 주소의 일부로 본다.
    """
    index = start
    in_path = False
    segment_started_non_ascii: bool | None = None
    scheme_end = text.find("://", start)
    host_start = scheme_end + 3 if scheme_end == start + 4 or scheme_end == start + 5 else start

    while index < len(text):
        char = text[index]
        if char in _URL_STOP:
            break

        if char in "/?#" and index >= host_start:
            in_path = True
            segment_started_non_ascii = None
            index += 1
            continue

        if ord(char) > 127:
            if not in_path:
                break  # 호스트에는 한글이 오지 않는다고 본다
            if segment_started_non_ascii is None:
                segment_started_non_ascii = True
            elif not segment_started_non_ascii:
                break  # 영문으로 시작한 조각에 한글이 붙었다
        elif in_path and segment_started_non_ascii is None:
            segment_started_non_ascii = False

        index += 1

    url = text[start:index]

    # 끝에 붙은 문장부호와 짝이 맞지 않는 닫는 괄호를 뗀다.
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
            continue
        if last in _BRACKETS and url.count(last) > url.count(_BRACKETS[last]):
            url = url[:-1]
            continue
        break

    if "." not in url.split("://")[-1].split("/")[0]:
        return index, ""
    return start + len(url), url


def _ensure_scheme(url: str) -> str:
    return url if re.match(r"^https?://", url, re.IGNORECASE) else "https://" + url


def _bare_host(host: str | None) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _naver_blog_post(host: str, parsed) -> tuple[str, str] | None:
    if host not in ("blog.naver.com", "m.blog.naver.com"):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 2 and segments[1].isdigit():
        return segments[0], segments[1]
    query = dict(parse_qsl(parsed.query))
    if query.get("blogId") and query.get("logNo", "").isdigit():
        return query["blogId"], query["logNo"]
    return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "classify",
    "extract_urls",
    "extract_video_id",
    "is_youtube",
    "is_short_link",
    "canonical_url",
    "cache_key",
    "login_wall_site",
    "MAX_INPUT_CHARS",
    "SHORT_LINK_HOSTS",
]
