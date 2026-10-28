#!/usr/bin/env bash
# 监视扫码登录：凭据写入 .env 即成功；进程消失即失败。
ENV=/root/dyonebot/douyin-spider/.env
LOG=/root/dyonebot/dy-bot/login_log.txt
for i in $(seq 1 720); do   # 最长约 60 分钟
  if grep -Eq "^DY_COOKIES=['\"]?[^#]*sessionid=[^; '\"]+" "$ENV" 2>/dev/null; then
    echo "RESULT=SUCCESS at $(date '+%F %T')"
    grep -E "^(DY_COOKIES|DY_TICKET|DY_TS_SIGN|DY_CLIENT_CERT|DY_PRIVATE_KEY|DY_DTRAIT_BLOB)=" "$ENV" \
      | sed -E "s/=(.{0,6}).*/=\1...(len=?) /"
    tail -12 "$LOG"
    exit 0
  fi
  if grep -qE "LOGIN_FAILED|LOGIN_OK" "$LOG" 2>/dev/null; then
    echo "RESULT=$(grep -oE 'LOGIN_FAILED.*|LOGIN_OK' "$LOG" | tail -1) at $(date '+%F %T')"
    tail -15 "$LOG"
    exit 0
  fi
  if ! pgrep -f "[l]ogin_qr.py" >/dev/null 2>&1; then
    echo "RESULT=PROCESS_GONE at $(date '+%F %T')"
    tail -15 "$LOG"
    exit 1
  fi
  sleep 5
done
echo "RESULT=TIMEOUT at $(date '+%F %T')"
exit 2
