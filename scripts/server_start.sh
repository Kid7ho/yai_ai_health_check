#!/usr/bin/env bash
# 스킬 서버와 ngrok 고정 주소를 켠다.
#
# 이 스크립트를 실행한 순간부터 카카오 채널 요청을 받는다. 자동 실행은 하지 않는다.
# 터미널을 닫아도 계속 돌고, scripts/server_stop.sh를 실행하거나 컴퓨터를 끄면 닫힌다.
# 컴퓨터를 다시 켜도 저절로 켜지지 않는다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/.run"
PORT="${PORT:-8000}"
mkdir -p "$RUN"

# .env에서 값 하나를 읽는다. 키 값은 화면에 찍지 않는다.
env_value() {
  grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'" || true
}

# pid 파일의 프로세스가 살아 있고 이름도 맞는지 본다. 재부팅 뒤 같은 번호를
# 다른 프로그램이 받았을 수 있어서 번호만 보지 않는다.
alive() {
  local pidfile="$RUN/$1.pid" pattern="$2" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile")"
  ps -p "$pid" -o command= 2>/dev/null | grep -q "$pattern"
}

DOMAIN="${NGROK_DOMAIN:-$(env_value NGROK_DOMAIN)}"
if [ -z "$DOMAIN" ]; then
  echo "NGROK_DOMAIN이 없습니다. .env에 NGROK_DOMAIN=배정받은주소.ngrok-free.dev 를 넣어 주세요."
  exit 1
fi

if alive server uvicorn || alive ngrok ngrok; then
  echo "이미 켜져 있습니다: https://$DOMAIN/health"
  echo "끄려면: scripts/server_stop.sh"
  exit 0
fi

command -v ngrok >/dev/null || { echo "ngrok이 없습니다. brew install ngrok 후 토큰을 등록해 주세요."; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "$ROOT/.venv 가 없습니다. 가상환경부터 만들어 주세요."; exit 1; }

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "포트 $PORT를 다른 프로그램이 쓰고 있습니다. 먼저 종료해 주세요."
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN
  exit 1
fi

echo "서버를 켭니다..."
cd "$ROOT"
nohup .venv/bin/python -m uvicorn healthcheck.api.app:app \
  --host 127.0.0.1 --port "$PORT" --app-dir src >>"$RUN/server.log" 2>&1 &
echo $! >"$RUN/server.pid"

for _ in $(seq 1 30); do
  curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "서버가 켜지지 않았습니다. 기록: $RUN/server.log"
  tail -5 "$RUN/server.log"
  "$ROOT/scripts/server_stop.sh" >/dev/null
  exit 1
fi

echo "고정 주소를 연결합니다..."
nohup ngrok http --url="$DOMAIN" "$PORT" --log=stdout --log-format=logfmt >>"$RUN/ngrok.log" 2>&1 &
echo $! >"$RUN/ngrok.pid"

# 잠자기 방지. -w로 서버 프로세스를 지켜보다가 서버가 끝나면 함께 끝난다.
nohup caffeinate -is -w "$(cat "$RUN/server.pid")" >/dev/null 2>&1 &
echo $! >"$RUN/awake.pid"

URL="https://$DOMAIN"
for _ in $(seq 1 30); do
  curl -fsS -m 5 "$URL/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS -m 5 "$URL/health" >/dev/null 2>&1; then
  echo "서버는 켜졌지만 고정 주소 연결에 실패했습니다. 기록: $RUN/ngrok.log"
  grep -E "lvl=(eror|crit)" "$RUN/ngrok.log" | tail -3 || true
  "$ROOT/scripts/server_stop.sh" >/dev/null
  exit 1
fi

echo
echo "켜졌습니다."
echo "  상태:     $URL/health"
SKILL_PATH_VALUE="$(env_value SKILL_PATH)"
echo "  스킬 URL: $URL${SKILL_PATH_VALUE:-/skill}"
echo "  기록:     $RUN/server.log"
echo "  끄기:     scripts/server_stop.sh"
echo
echo "전원 어댑터를 연결하고 덮개를 열어 두세요. 잠들면 요청을 받지 못합니다."
