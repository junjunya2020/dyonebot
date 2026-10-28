# -*- coding: utf-8 -*-
"""抖音私信自动回复机器人（仓库外部脚本，不改动任何项目源码）。

链路：
  1. 用 .env 里的登录凭证建立 DouyinAuth
  2. 连 frontier-im WebSocket 收私信
  3. 收到文本消息 -> 用 sender uid 调 create_conversation 换取 ticket
  4. 用拿到的 ticket 调 send_msg 回复

用法：
  cd /root/dyonebot/douyin-spider
  PYTHONPATH=/root/dyonebot/douyin-spider .venv/bin/python -u /root/dyonebot/dy-bot/autoreply_bot.py

回复规则在 decide_reply() 里，默认是固定话术 + 关键词匹配。
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/dyonebot/douyin-spider")
os.chdir("/root/dyonebot/douyin-spider")

from dy_apis.douyin_api import DouyinAPI
from dy_apis.douyin_recv_msg import DouyinRecvMsg
from static import Live_pb2, Response_pb2
from utils.common_util import load_env

LOG = Path("/root/dyonebot/dy-bot/bot_log.txt")

# ---------------------------------------------------------------- 回复规则

DEFAULT_REPLY = "你好，我是自动回复机器人，稍后人工会回复你～"

KEYWORD_REPLIES = [
    (["价格", "多少钱", "费用"], "价格问题我记下啦，人工客服会尽快联系你～"),
    (["你好", "在吗", "hi", "hello", "在么"], "在的，请问有什么可以帮你？"),
    (["人工", "客服"], "已为你转接人工，请稍等～"),
]


def decide_reply(text: str) -> str:
    low = (text or "").strip().lower()
    for keys, reply in KEYWORD_REPLIES:
        if any(k.lower() in low for k in keys):
            return reply
    return DEFAULT_REPLY


# ---------------------------------------------------------------- 日志

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- 机器人

class AutoReplyBot:
    def __init__(self, auth, dry_run=False):
        self.auth = auth
        self.dry_run = dry_run
        self.my_uid = None
        try:
            self.my_uid = str(auth.get_uid())
        except Exception as err:
            log(f"WARN 取自己 uid 失败: {err}")
        # 记住每个会话的 ticket，避免每条消息都重开一次会话
        self.ticket_cache = {}

    # ---- 拿 ticket -------------------------------------------------
    def get_ticket(self, peer_uid):
        """用对方 uid 换 (conversation_id, conversation_short_id, ticket)。

        create_conversation 对已存在的会话是幂等的：会返回同一个会话和新的 ticket。
        """
        cached = self.ticket_cache.get(str(peer_uid))
        if cached and time.time() - cached["ts"] < 300:
            return cached["conversation_id"], cached["short_id"], cached["ticket"]

        conversation_id, short_id, ticket = DouyinAPI.create_conversation(
            self.auth, int(peer_uid)
        )
        self.ticket_cache[str(peer_uid)] = {
            "conversation_id": conversation_id,
            "short_id": short_id,
            "ticket": ticket,
            "ts": time.time(),
        }
        log(f"换到 ticket peer={peer_uid} conv={conversation_id}")
        return conversation_id, short_id, ticket

    # ---- 发回复 ----------------------------------------------------
    def reply(self, peer_uid, reply_text, fallback_conv=None, fallback_short=None):
        if self.dry_run:
            log(f"DRY-RUN 本该回复 peer={peer_uid}: {reply_text}")
            return True
        try:
            conversation_id, short_id, ticket = self.get_ticket(peer_uid)
        except Exception as err:
            log(f"ERROR 换 ticket 失败 peer={peer_uid}: {type(err).__name__}: {err}")
            # 退路：用收到消息里带的会话 id，但 ticket 仍然缺，只能放弃
            if fallback_conv:
                log(f"  收到消息里的会话 {fallback_conv} 无 ticket，本条放弃")
            return False

        try:
            ok = DouyinAPI.send_msg(
                self.auth, conversation_id, int(short_id), ticket, reply_text
            )
            log(f"{'OK  ' if ok else 'FAIL'} 回复 peer={peer_uid}: {reply_text}")
            return bool(ok)
        except Exception as err:
            log(f"ERROR 发送失败 peer={peer_uid}: {type(err).__name__}: {err}")
            self.ticket_cache.pop(str(peer_uid), None)
            return False

    # ---- 消息处理 --------------------------------------------------
    def handle_text(self, sender, text, conversation_id, short_id):
        if self.my_uid and str(sender) == self.my_uid:
            log(f"跳过自己发的消息: {text}")
            return
        reply_text = decide_reply(text)
        log(f"收到 peer={sender} 内容={text!r} -> 回复={reply_text!r}")
        self.reply(sender, reply_text, conversation_id, short_id)


def env_has_credentials():
    """先直接读 .env，判断有没有登录凭据。

    不能靠 load_env() 的返回值判断：DY_COOKIES 为空时 load_env() 会
    自己发起一次扫码登录并阻塞，等于在机器人里又开了一个抢二维码的登录流。
    """
    env_file = Path("/root/dyonebot/douyin-spider/.env")
    if not env_file.exists():
        return False
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DY_COOKIES="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            return bool(value)
    return False


def main():
    dry_run = os.environ.get("DYBOT_DRY_RUN", "") not in ("", "0", "false")
    log("=" * 50)
    log(f"bot start dry_run={dry_run}")

    if not env_has_credentials():
        log("ABORT .env 里没有登录凭据（DY_COOKIES 为空）。")
        log("      请先完成扫码登录：/root/dyonebot/dy-bot/start_login.sh")
        return 1

    auth = load_env()
    if not (auth.cookie or {}).get("sessionid"):
        log("ABORT 凭据不完整：cookie 里没有 sessionid，请重新登录")
        return 1
    log(f"凭证 OK cookie_count={len(auth.cookie)}")
    log(f"ticket={bool(auth.ticket)} ts_sign={bool(auth.ts_sign)} "
        f"private_key={bool(auth.private_key)}")

    bot = AutoReplyBot(auth, dry_run=dry_run)

    recv = DouyinRecvMsg(auth)

    # 覆盖 on_message：复用官方解析，但把结果交给机器人
    def on_message(ws, message):
        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(message)
            if frame.payloadType != 'pb':
                return
            response = Response_pb2.Response()
            response.ParseFromString(frame.payload)
            msg = response.body.new_message_notify.message
            sender = msg.sender
            msg_type = msg.message_type
            conversation_id = msg.conversation_id
            short_id = msg.conversation_short_id
            if int(msg_type) != 7:          # 7 = 文本
                log(f"忽略非文本消息 type={msg_type} peer={sender}")
                return
            content = json.loads(msg.content)
            bot.handle_text(sender, content.get("text", ""), conversation_id, short_id)
        except Exception as err:
            log(f"ERROR 处理消息异常: {type(err).__name__}: {err}")

    recv.on_message = on_message
    log("监听私信中… (Ctrl-C 停止)")
    try:
        recv.start()
    except KeyboardInterrupt:
        log("bot stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
