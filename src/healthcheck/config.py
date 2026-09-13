"""환경 변수 설정.

API 키는 코드에도, 저장소에도 두지 않는다. 프로세스 환경 변수로만 받는다.
로컬 개발에서는 .env 파일에 넣는다. .env는 .gitignore에 들어 있어 커밋되지 않는다.

키를 로그로 남기지 않는다. 실수로 남기는 일을 막기 위해 __repr__에서
키 값을 가린다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

AdapterMode = Literal["fake", "live"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True, slots=True)
class Settings:
    """실행에 필요한 설정 전부."""

    # fake이면 Fake 어댑터를 꽂는다. live이면 실제 API를 호출한다.
    adapter_mode: AdapterMode = "fake"
    liner_api_key: str | None = field(default=None, repr=False)
    mfds_api_key: str | None = field(default=None, repr=False)
    rules_path: Path = PROJECT_ROOT / "rules" / "verdict_rules_v0.yaml"
    worker_count: int = 2

    # 자막 추출용 프록시. YouTube는 클라우드 제공자 IP를 대부분 차단한다.
    # 둘 중 하나만 설정한다. 비워 두면 서버 IP로 직접 호출한다.
    youtube_proxy_url: str | None = field(default=None, repr=False)
    webshare_username: str | None = field(default=None, repr=False)
    webshare_password: str | None = field(default=None, repr=False)

    # 자막 호출 간 최소 간격(초). 짧은 시간에 몰아 부르면 차단을 부른다.
    transcript_min_interval: float = 1.5
    # 차단이 감지된 뒤 자막 호출을 멈추는 시간(초).
    transcript_cooldown: float = 600.0

    # 디버그 채팅 화면(/debug)을 띄울지. 개발용이며 배포 시에는 끈다.
    debug_ui: bool = True

    # 자막이 없는 영상을 음성 인식으로 처리할지. 기본은 꺼짐이다.
    # 켜면 응답이 분 단위로 늦어지고 첫 실행에서 모델을 내려받는다.
    asr_enabled: bool = False
    asr_model_size: str = "small"
    # 음성 인식할 최대 길이(초). 0이면 영상 전체. 길면 그만큼 오래 걸린다.
    asr_max_seconds: int = 300

    # 기사·블로그 링크에서 본문을 뽑을지.
    article_enabled: bool = True

    # 로그 수준. 기본 INFO다. 이 설정이 없으면 판정 로그와 조회 로그가 전부
    # 버려진다. uvicorn은 자기 로거만 설정하고 우리 로거는 건드리지 않는다.
    log_level: str = "INFO"

    # 카카오 요청 본문을 로그에 남길지. 연동 초기에 실제 구조를 확인하는 용도다.
    # 사용자 식별자는 가려서 남기지만 발화 내용이 로그에 들어가므로 연동이
    # 끝나면 꺼야 한다.
    log_skill_request: bool = False

    # 스킬 엔드포인트 인증. 공개 주소로 노출하면 최소 하나는 켜야 한다.
    # 인증이 없으면 주소를 아는 누구나 파이프라인을 호출한다.
    skill_auth_header: str = "X-Skill-Token"
    skill_auth_token: str | None = field(default=None, repr=False)
    # 엔드포인트 경로. 헤더를 지정할 수 없을 때 추측하기 어려운 값으로 바꾼다.
    skill_path: str = "/skill"

    # 저장소. 판정 기록과 목표 지표가 여기 쌓인다.
    database_path: str = "healthcheck.db"
    # 개인정보 보존 기간(일). README의 저장 데이터 표와 같은 값이다.
    text_retention_days: int = 90
    user_key_retention_days: int = 30

    @property
    def has_liner_key(self) -> bool:
        return bool(self.liner_api_key)

    @property
    def has_mfds_key(self) -> bool:
        return bool(self.mfds_api_key)

    @property
    def has_skill_token(self) -> bool:
        return bool(self.skill_auth_token)

    @property
    def has_youtube_proxy(self) -> bool:
        return bool(self.youtube_proxy_url or (self.webshare_username and self.webshare_password))

    def masked(self) -> dict[str, str]:
        """기동 로그에 남길 요약. 키 값은 절대 넣지 않는다."""
        return {
            "adapter_mode": self.adapter_mode,
            "liner_api_key": "설정됨" if self.has_liner_key else "없음",
            "mfds_api_key": "설정됨" if self.has_mfds_key else "없음",
            "rules_path": str(self.rules_path),
            "youtube_proxy": "설정됨" if self.has_youtube_proxy else "없음",
            "skill_auth_token": "설정됨" if self.has_skill_token else "없음",
            "skill_path": self.skill_path,
        }


def load_dotenv(path: Path | str = DEFAULT_ENV_FILE) -> None:
    """.env를 읽어 환경 변수에 올린다.

    이미 설정된 환경 변수를 덮어쓰지 않는다. 배포 환경에서 주입한 값이
    저장소에 남아 있던 파일에 밀리면 안 된다.

    의존성을 늘리지 않기 위해 python-dotenv를 쓰지 않고 직접 읽는다.
    형식은 KEY=VALUE 한 줄씩이고 #로 시작하는 줄은 주석이다.
    """
    path = Path(path)
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(env_file: Path | str | None = DEFAULT_ENV_FILE) -> Settings:
    """환경 변수에서 설정을 읽는다."""
    if env_file is not None:
        load_dotenv(env_file)

    mode = (os.environ.get("ADAPTER_MODE") or "fake").strip().lower()
    if mode not in ("fake", "live"):
        raise ValueError(f"ADAPTER_MODE는 fake 또는 live여야 합니다: {mode!r}")

    return Settings(
        adapter_mode=mode,  # type: ignore[arg-type]
        liner_api_key=_clean(os.environ.get("LINER_API_KEY")),
        mfds_api_key=_clean(os.environ.get("MFDS_API_KEY")),
        worker_count=int(os.environ.get("WORKER_COUNT") or 2),
        youtube_proxy_url=_clean(os.environ.get("YOUTUBE_PROXY_URL")),
        webshare_username=_clean(os.environ.get("WEBSHARE_PROXY_USERNAME")),
        webshare_password=_clean(os.environ.get("WEBSHARE_PROXY_PASSWORD")),
        transcript_min_interval=float(os.environ.get("TRANSCRIPT_MIN_INTERVAL") or 1.5),
        transcript_cooldown=float(os.environ.get("TRANSCRIPT_COOLDOWN") or 600),
        debug_ui=_flag(os.environ.get("DEBUG_UI"), True),
        asr_enabled=_flag(os.environ.get("ASR_ENABLED"), False),
        asr_model_size=(os.environ.get("ASR_MODEL_SIZE") or "small").strip(),
        asr_max_seconds=int(os.environ.get("ASR_MAX_SECONDS") or 300),
        article_enabled=_flag(os.environ.get("ARTICLE_ENABLED"), True),
        log_level=(os.environ.get("LOG_LEVEL") or "INFO").strip().upper(),
        log_skill_request=_flag(os.environ.get("LOG_SKILL_REQUEST"), False),
        skill_auth_header=(os.environ.get("SKILL_AUTH_HEADER") or "X-Skill-Token").strip(),
        skill_auth_token=_clean(os.environ.get("SKILL_AUTH_TOKEN")),
        skill_path=_normalize_path(os.environ.get("SKILL_PATH")),
        database_path=(os.environ.get("DATABASE_PATH") or "healthcheck.db").strip(),
        text_retention_days=int(os.environ.get("TEXT_RETENTION_DAYS") or 90),
        user_key_retention_days=int(os.environ.get("USER_KEY_RETENTION_DAYS") or 30),
    )


def _normalize_path(value: str | None) -> str:
    """경로는 항상 슬래시로 시작하게 맞춘다. 설정 실수로 라우팅이 깨지지 않게 한다."""
    path = (value or "/skill").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/skill"


def _flag(value: str | None, default: bool) -> bool:
    """켜짐·꺼짐 환경 변수. 비어 있으면 기본값을 쓴다."""
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _clean(value: str | None) -> str | None:
    """빈 문자열은 없는 것으로 취급한다. .env에 키 이름만 써 둔 경우다."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def setup_logging(level: str = "INFO") -> None:
    """healthcheck 로거에 출력 핸들러를 붙인다.

    uvicorn은 자기 로거만 설정한다. 우리 모듈의 logger.info는 기본 수준이
    WARNING이라 전부 버려졌다. 판정 로그, 조회 로그, 캐시 적중 로그가 모두
    여기에 해당한다. 운영에서 무슨 일이 일어났는지 보려면 필요하다.
    """
    import logging

    logger = logging.getLogger("healthcheck")
    logger.setLevel(getattr(logging, level, logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        logger.addHandler(handler)
    # 루트로 다시 올려 보내지 않는다. uvicorn 핸들러와 겹쳐 두 번 찍힌다.
    logger.propagate = False
