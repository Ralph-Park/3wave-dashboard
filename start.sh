#!/usr/bin/env bash
# 3-Wave Dashboard 실행 스크립트
# 사용법:  ./start.sh          (기본 포트 8765)
#          ./start.sh 9000     (포트 지정)
#
# index.html 을 file:// 로 직접 열면 브라우저 보안 정책 때문에 JSON 을 못 읽는다.
# 반드시 로컬 HTTP 서버로 띄워야 한다.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
URL="http://localhost:${PORT}/index.html"

# 이미 떠 있으면 새로 띄우지 않는다
if curl -s -o /dev/null -m 2 "$URL"; then
  echo "이미 실행 중입니다 → $URL"
else
  echo "서버를 시작합니다 (포트 ${PORT})..."
  nohup python3 -m http.server "$PORT" >/tmp/3wave-http-${PORT}.log 2>&1 &
  for _ in $(seq 1 20); do
    sleep 0.3
    curl -s -o /dev/null -m 2 "$URL" && break
  done
fi

echo "대시보드 주소: $URL"
if command -v open >/dev/null 2>&1; then
  open "$URL"          # macOS 기본 브라우저로 열기
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
fi
echo
echo "서버를 끄려면:  pkill -f 'http.server ${PORT}'"
