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

# 의존성만 먼저 설치해 레이어 캐시를 살린다.
# 패키지 자체(`.`)를 설치하면 소스가 아직 없어 빌드가 실패하고, 설치되더라도
# site-packages 쪽 healthcheck가 잡혀 규칙표 경로(PROJECT_ROOT)가 어긋난다.
# 코드는 PYTHONPATH로 /app/src에서 읽는다.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

# 규칙표는 코드가 아니라 데이터다. 의료 자문의 검수 결과를 반영할 때
# 이미지를 다시 굽지 않도록 볼륨으로 덮어쓸 수 있게 따로 복사한다.
COPY rules/ ./rules/
COPY src/ ./src/
# 팀원 분류 시트. LABELING_TOKEN이 있을 때만 서버가 연다.
COPY labeling/claim_labeling.html ./labeling/claim_labeling.html
# 성과 요약 스크립트. 서버에서 판정 DB를 읽어 집계만 찍는다(fly ssh console -C).
COPY scripts/report_metrics.py ./scripts/report_metrics.py

ENV PYTHONPATH=/app/src \
    ADAPTER_MODE=fake

EXPOSE 8000

# 카카오 스킬 응답 제한이 5초다. 워커는 앱 수명주기가 함께 띄운다.
# 작업 큐와 Liner 호출 제한이 프로세스 안에 있으므로 워커 프로세스는 하나만 띄운다.
CMD ["uvicorn", "healthcheck.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
