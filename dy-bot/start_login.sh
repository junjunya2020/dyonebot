#!/usr/bin/env bash
# 常驻扫码登录：单次窗口到期就自动重开，直到扫码成功为止。
# 放在仓库外，不改动任何项目源码。
set -u

REPO=/root/dyonebot/douyin-spider
SCRIPT=/root/dyonebot/dy-bot/login_qr.py
LOG=/root/dyonebot/dy-bot/login_loop.log

cd "$REPO" || exit 1

has_session() {
  # Accept dotenv's quoted form and ordinary KEY=VALUE form.  Only a
  # non-empty sessionid counts; merely having DY_COOKIES='' must not stop the
  # login loop.
  grep -Eq "^DY_COOKIES=['\"]?[^#]*sessionid=[^; '\"]+" "$REPO/.env" 2>/dev/null
}

if has_session; then
  echo "[$(date '+%F %T')] existing login credentials found, no QR needed" >> "$LOG"
  exit 0
fi

echo "[$(date '+%F %T')] login loop start" >> "$LOG"
attempt=0
while true; do
  attempt=$((attempt + 1))
  echo "[$(date '+%F %T')] --- attempt #$attempt ---" >> "$LOG"

  PYTHONPATH="$REPO" DYBOT_QR_TIMEOUT="${DYBOT_QR_TIMEOUT:-1800}" \
    .venv/bin/python -u "$SCRIPT" >> "$LOG" 2>&1

  # 扫码成功会写出凭据，此时 .env 里的 DY_COOKIES 不再为空
  if has_session; then
    echo "[$(date '+%F %T')] LOGIN SUCCESS, loop exit" >> "$LOG"
    exit 0
  fi

  echo "[$(date '+%F %T')] attempt #$attempt 未扫码，2 秒后重开" >> "$LOG"
  sleep 2
done
