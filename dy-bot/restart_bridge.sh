#!/usr/bin/env bash
# 重启 OneBot 桥：先停旧的，再以新配置启动。
# 单独成文件，避免 pkill -f 匹配到调用者自己的命令行。
set -u

REPO=/root/dyonebot/douyin-spider
ENTRY=/root/dyonebot/dy-bot/run_bridge.py
LOG=/root/dyonebot/dy-bot/logs/bridge.stdout.log
# Match the actual bridge command, not any shell command that merely mentions
# run_bridge.py (the old broad pkill could kill this script itself).
PAT='^(.*/)?\.venv/bin/python -u /root/dyonebot/dy-bot/run_bridge\.py$'

stop_old() {
  pkill -f "$PAT" 2>/dev/null
  for _ in $(seq 1 15); do
    pgrep -f "$PAT" >/dev/null 2>&1 || return 0
    sleep 1
  done
  pkill -9 -f "$PAT" 2>/dev/null
  sleep 1
}

stop_old
: > "$LOG"
cd "$REPO" || exit 1
setsid nohup .venv/bin/python -u "$ENTRY" >> "$LOG" 2>&1 < /dev/null &
sleep 8
echo "bridge pid: $(pgrep -f "$PAT" | head -1)"
