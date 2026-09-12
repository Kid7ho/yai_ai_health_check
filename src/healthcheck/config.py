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

    @property
    def has_liner_key(self) -> bool:
        return bool(self.liner_api_key)

    @property
    def has_mfds_key(self) -> bool:
        return bool(self.mfds_api_key)

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
    )


def _clean(value: str | None) -> str | None:
    """빈 문자열은 없는 것으로 취급한다. .env에 키 이름만 써 둔 경우다."""
    if value is None:
        return None
    value = value.strip()
    return value or None
