#!/usr/bin/env bash
# scripts/server_start.sh로 켠 서버, ngrok, 잠자기 방지를 끈다.
#
# 이 스크립트가 켠 프로세스만 끈다. pid 파일에 적힌 번호와 프로그램 이름이
# 모두 맞을 때만 종료 신호를 보낸다.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/.run"
PORT="${PORT:-8000}"

stop_one() {
  local name="$1" pattern="$2" label="$3" pidfile="$RUN/$1.pid" pid
  if [ ! -f "$pidfile" ]; then
    return
  fi
  pid="$(cat "$pidfile")"
  if ! ps -p "$pid" -o command= 2>/dev/null | grep -q "$pattern"; then
    rm -f "$pidfile"
    return
  fi

  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null
  fi
  rm -f "$pidfile"
  echo "  $label 종료"
}

# 처리 중인 작업은 콜백을 보내지 못하고 사라진다. 끄기 전에 알려 준다.
queued="$(curl -fsS -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | sed -nE 's/.*"queued":([0-9]+).*/\1/p')"
if [ -n "${queued:-}" ] && [ "$queued" != "0" ]; then
  echo "대기 중인 확인 요청 ${queued}건은 결과를 보내지 못합니다."
fi

echo "서버를 끕니다..."
# 외부 입구부터 닫는다. 서버를 먼저 끄면 그 사이 들어온 요청이 오류를 받는다.
stop_one ngrok ngrok "고정 주소(ngrok)"
stop_one server uvicorn "스킬 서버"
stop_one awake caffeinate "잠자기 방지"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "포트 $PORT를 아직 다른 프로그램이 쓰고 있습니다. 이 스크립트가 켠 서버가 아닙니다."
  exit 1
fi
echo "꺼졌습니다."
