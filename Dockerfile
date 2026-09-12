# 특정 PaaS에 묶이지 않게 컨테이너로 감싼다.
FROM python:3.11-slim

# uv를 쓴다. lock과 가상환경과 파이썬 버전을 한 도구로 처리해서
# 빌드 재현성이 좋고 설치 단계가 짧다.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 의존성 먼저 설치해 레이어 캐시를 살린다.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .

# 규칙표는 코드가 아니라 데이터다. 의료 자문의 검수 결과를 반영할 때
# 이미지를 다시 굽지 않도록 볼륨으로 덮어쓸 수 있게 따로 복사한다.
COPY rules/ ./rules/
COPY src/ ./src/

ENV PYTHONPATH=/app/src \
    ADAPTER_MODE=fake

EXPOSE 8000

# 카카오 스킬 응답 제한이 5초다. 워커는 앱 수명주기가 함께 띄운다.
CMD ["uvicorn", "healthcheck.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
