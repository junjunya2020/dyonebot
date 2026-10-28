# -*- coding: utf-8 -*-
"""桥的自测：不连抖音、不连 AstrBot，用桩件把整条协议链跑一遍。

覆盖：
  1. 正向 WS 握手 + 事件推送 + action 回执（echo 保真）
  2. 反向 WS 连出去（本脚本自己当服务端）
  3. HTTP API POST /<action>
  4. 事件构造：群聊/私聊/撤回/系统通知过滤/自己发的消息过滤
  5. action 路由：发送、撤回、群成员、群信息、好友列表、用户资料
  6. 未实现 action 回 failed/1404（绝不沉默）

用法：
  /root/dyonebot/douyin-spider/.venv/bin/python /root/dyonebot/dy-bot/onebot_bridge_selftest.py
"""
import asyncio
import json
import sys
import threading
import time

import websockets

sys.path.insert(0, "/root/dyonebot/dy-bot")
sys.path.insert(0, "/root/dyonebot/douyin-spider")

from onebot_bridge import cq  # noqa: E402
from onebot_bridge.bridge import Bridge, EventBuilder, build_self_id  # noqa: E402
from onebot_bridge.ids import IdMap  # noqa: E402

SELF_UID = "3669513203620634"
PEER_UID = "2692024583072670"
GROUP_ID = "7689446943537234469"
GROUP_INT = int(GROUP_ID)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)


# ---------------------------------------------------------------- 桩件

class StubIM:
    """假装自己是抖音。所有调用都记账，方便断言。"""

    def __init__(self):
        self.auth = object()
        self.self_uid = SELF_UID
        self.calls = []
        self.sent = []

    def get_ticket(self, conversation_id, conversation_type, short_id=None,
                   force=False):
        self.calls.append(("get_ticket", str(conversation_id), int(conversation_type)))
        return {"conv_id": str(conversation_id), "short_id": int(short_id or 1),
                "ticket": "tkt", "ctype": int(conversation_type), "ts": time.time()}

    def send(self, conversation_id, conversation_type, message_type, content,
             mentioned_users=None, ref=None, client_message_id=None, short_id=None):
        self.calls.append(("send", str(conversation_id), int(conversation_type),
                           int(message_type),
                           content.get("text") if isinstance(content, dict) else content))
        self.sent.append({"conv": str(conversation_id), "type": int(message_type),
                          "content": content, "mentioned": mentioned_users,
                          "ref": ref})
        return True, 999000111222333, "ok"

    def recall(self, conversation_id, conversation_type, server_message_id,
               short_id=None):
        self.calls.append(("recall", str(conversation_id), int(server_message_id)))
        return True, "ok"

    def conversation_info(self, conversation_id, conversation_type=2, short_id=None):
        self.calls.append(("conversation_info", str(conversation_id)))
        return {"conversation_id": GROUP_ID, "short_id": GROUP_ID, "type": 2,
                "ticket": "t", "name": "ai聊天群", "icon": "http://x/y.jpg",
                "owner": "2916383924691644", "sec_owner": "SEC",
                "participants_count": 4, "online_member_count": 1}

    def participants(self, conversation_id, conversation_type=2, short_id=None,
                     limit=200):
        self.calls.append(("participants", str(conversation_id)))
        return [
            {"user_id": "2916383924691644", "sec_uid": "S1", "alias": "",
             "role": 1, "role_name": "owner", "blocked": 0},
            {"user_id": SELF_UID, "sec_uid": "S2", "alias": "",
             "role": 0, "role_name": "member", "blocked": 0},
            {"user_id": PEER_UID, "sec_uid": "S3", "alias": "小名",
             "role": 2, "role_name": "admin", "blocked": 0},
        ]

    def conversation_list(self, con_type, limit=100, cursor=0, with_cold=1):
        self.calls.append(("conversation_list", int(con_type)))
        if int(con_type) == 2:
            return ([{"conversation_id": GROUP_ID,
                      "conversation_short_id": GROUP_ID, "type": 2,
                      "name": "ai聊天群", "icon": "", "owner": "2916383924691644",
                      "participant_count": 4, "unread_count": 0, "ticket": "t",
                      "peer": ""}], False, 1)
        return ([{"conversation_id": f"0:1:{PEER_UID}:{SELF_UID}",
                  "conversation_short_id": "7689477204626932273", "type": 1,
                  "name": "", "icon": "", "owner": SELF_UID,
                  "participant_count": 2, "unread_count": 0, "ticket": "t",
                  "peer": PEER_UID}], False, 1)

    def profile(self, uid=None, sec_uid=None, use_cache=True):
        self.calls.append(("profile", str(uid or sec_uid)))
        names = {"2916383924691644": "均", PEER_UID: "Febtt", SELF_UID: ".ccc"}
        key = str(uid or "")
        return {"user_id": key, "sec_uid": sec_uid or "S", "nickname": names.get(key, ""),
                "avatar": "", "ts": time.time()}


class StubRecv:
    """替换上游的 DouyinRecvMsg：不连抖音，只保存注入点。"""

    instance = None

    def __init__(self, auth, auto_reconnect=True):
        self.auth = auth
        self.on_message = None
        self.on_open = None
        self.on_error = None
        self.on_close = None
        self.started = asyncio.Event() if False else None
        StubRecv.instance = self

    def start(self):
        if self.on_open:
            self.on_open(self)
        # 阻塞线程，直到测试结束（和真实实现一样常驻）
        while not StubRecv._stop:
            time.sleep(0.1)

    _stop = False


# ---------------------------------------------------------------- 事件样本

_SID_SEQ = [7700000000000000000]


def next_sid():
    """每条样本消息一个唯一 server_message_id。

    必须唯一：桥用 server_message_id 当 OneBot message_id 并做去重，
    复用同一个 id 会被正确地去重掉，测试会误判成「事件丢了」。
    """
    _SID_SEQ[0] += 1
    return _SID_SEQ[0]


def dm_msg(mt=7, sender=SELF_UID, conv=GROUP_ID, ctype=2, content=None, sid=None,
           ext=None):
    return {
        "conversation_id": conv, "conversation_type": ctype, "message_type": mt,
        "sender": sender, "sender_sec_uid": "SEC",
        "server_message_id": sid or next_sid(),
        "short_id": int(conv) if conv.isdigit() else 1, "index": 1,
        "content": content if content is not None else {"aweType": 700, "text": "你好"},
        "ext": ext or {}, "create_time": 1790345282,
    }


# ---------------------------------------------------------------- 分页护栏

def check_pagination():
    """回归：cmd 2006 的 limit > 50 时抖音不报错、静默回空列表。

    这里用一个假的 _post 精确复刻该行为（limit<=50 回页，limit>50 回空），
    再断言 conversation_list 会把 limit 夹到 50 且只发一发请求。

    注意：这个接口只服务枚举类能力（群列表/好友列表/昵称预取）。
    真实消息收发走 WS 推送，和它无关；会话多到几百上千时每次全量拉也不现实，
    所以【不做翻页】，取最近 50 条即可。
    """
    from onebot_bridge.dyim import (CONV_ONE_TO_ONE, DouyinIM)
    from Im_pb2 import Response as ImResponse

    CONVS = [f"0:1:{1000 + i}:{SELF_UID}" for i in range(12)]

    class FakeIM:
        _uid = SELF_UID
        # 这里必须和 DouyinIM 保持同样的协作面，否则会抛 AttributeError，
        # 而异常在 asyncio.run 收尾时会被 StubRecv 常驻线程拖住，表现为假死。
        CONVERSATION_PAGE_MAX = DouyinIM.CONVERSATION_PAGE_MAX
        peer_of = staticmethod(DouyinIM.peer_of)

        def __init__(self):
            self.calls = []

        def _post(self, cmd, path, fill):
            from Im_pb2 import Request as ImRequest
            env = ImRequest()
            fill(env)
            b = env.body.get_user_conversation_list_body
            self.calls.append((int(b.cursor), int(b.limit)))
            rp = ImResponse()
            rp.cmd = 2006
            rp.message = "OK"
            page = rp.body.get_user_conversation_list_body
            if b.limit <= 50:
                chunk = CONVS[b.cursor:b.cursor + b.limit]
                for cid in chunk:
                    c = page.conversations.add()
                    c.conversation_id = cid
                    c.conversation_short_id = int(cid.split(":")[2])
                    c.conversation_type = CONV_ONE_TO_ONE
                page.has_more = b.cursor + b.limit < len(CONVS)
                page.total = len(CONVS)
            return rp

    # 传 200 也必须夹到 50，而且只打一发（不翻页）
    fake = FakeIM()
    rows, _more, _total = DouyinIM.conversation_list(
        fake, CONV_ONE_TO_ONE, limit=200)
    record("conversation_list 把 limit 夹到 50 且只打一发",
           all(lim <= 50 for _c, lim in fake.calls) and len(fake.calls) == 1
           and len(rows) == 12,
           f"n={len(rows)} 请求={fake.calls}")

    # 小 limit 原样透传
    fake2 = FakeIM()
    rows2, _m, _t = DouyinIM.conversation_list(fake2, CONV_ONE_TO_ONE, limit=5)
    record("conversation_list 小 limit 原样透传",
           len(rows2) == 5 and fake2.calls == [(0, 5)],
           f"n={len(rows2)} 请求={fake2.calls}")

    # 服务器谎报 has_more=True 时也不能再翻页（没有翻页逻辑，必然只有一发）
    class StuckIM(FakeIM):
        def _post(self, cmd, path, fill):
            rp = super()._post(cmd, path, fill)
            page = rp.body.get_user_conversation_list_body
            if page.conversations:
                page.has_more = True
            return rp

    stuck = StuckIM()
    rows3, _m, _t = DouyinIM.conversation_list(stuck, CONV_ONE_TO_ONE, limit=200)
    record("has_more=True 也不会额外翻页",
           len(rows3) == 12 and len(stuck.calls) == 1,
           f"n={len(rows3)} 请求数={len(stuck.calls)}")


# ---------------------------------------------------------------- 日志中枢护栏

def check_loghub():
    """回归：面板「日志查看」的数据源必须真的被装上。

    曾经的 bug：logutil.setup() 全项目没有任何调用点，于是 _installed 恒为
    False、get_hub() 恒为 None，面板日志页永远空（total=0）—— 而上游为什么
    掉线恰恰只能从这些行里看出来，等于把唯一的诊断入口焊死了。

    两件事都要钉住：
      a) 入口确实调了 setup（源码级护栏，这才是当初漏掉的那一步）
      b) 装上之后，三条来源（我们的 log() / 上游裸 print() / logging）都真的
         进了同一个环形缓冲，并且 SSE 订阅者收得到。
    """
    import logging
    import os
    import tempfile

    from onebot_bridge import logutil

    # ---- a) 源码级：入口必须调用 setup，否则面板必空
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_bridge.py")
    src = ""
    if os.path.exists(entry):
        with open(entry, encoding="utf-8") as fh:
            src = fh.read()
    record("run_bridge 确实调用了 logutil.setup()（漏了它面板就恒空）",
           "logutil.setup(" in src,
           "未找到调用点" if "logutil.setup(" not in src else "已接入")

    # ---- b) 运行时：装中枢，三种来源都要落进同一个缓冲
    fd, path = tempfile.mkstemp(prefix="dybot-selftest-", suffix=".log")
    os.close(fd)
    os.unlink(path)                      # 让 setup 自己建，顺便验证 makedirs

    saved_out, saved_err = sys.stdout, sys.stderr
    hub = None
    try:
        hub = logutil.setup(path, capacity=200, max_bytes=0)
        record("setup() 返回可用中枢且 get_hub() 一致",
               hub is not None and logutil.get_hub() is hub)

        # 1) 我们自己的 log()
        logutil.log("自测-中枢探针A", "INFO")
        # 2) 上游 douyin-spider 的裸 print()（它不 import 我们的 log）
        print("自测-中枢探针B", flush=True)
        # 3) 第三方库的 logging
        logging.getLogger("dybot.selftest").warning("自测-中枢探针C")

        rows = hub.tail(limit=200)
        by_msg = {r["msg"]: r for r in rows}
        record("log() 进中枢且级别正确",
               by_msg.get("自测-中枢探针A", {}).get("level") == "INFO",
               f"level={by_msg.get('自测-中枢探针A', {}).get('level')}")
        record("上游裸 print() 也被 Tee 捞进中枢（掉线日志就靠这条）",
               "自测-中枢探针B" in by_msg
               and by_msg["自测-中枢探针B"]["level"] == "OUT",
               f"level={by_msg.get('自测-中枢探针B', {}).get('level')}")
        record("logging.warning 进中枢",
               "自测-中枢探针C" in by_msg
               and by_msg["自测-中枢探针C"]["level"] == "WARN",
               f"level={by_msg.get('自测-中枢探针C', {}).get('level')}")

        # 级别过滤：WARN 视图不该再出现 INFO
        warn_only = hub.tail(limit=200, level="WARN")
        record("level=WARN 过滤掉 INFO 行",
               all(r["level"] in ("WARN", "ERR", "ERROR", "CRITICAL")
                   for r in warn_only)
               and any(r["msg"] == "自测-中枢探针C" for r in warn_only),
               f"n={len(warn_only)}")

        # seq 单调 + since_seq 增量拉取（WebUI 轮询靠它）
        # 注意：本自测自己的 record()/print 也会被 Tee 抄进中枢，所以基准线必须
        # 在两步之间现取，否则"应该是空的"会被自己刚打的那几行打破。
        mark = hub.stats()["last_seq"]
        newer = hub.tail(since_seq=mark)
        older = hub.tail(since_seq=mark - 1)
        record("since_seq 增量拉取只回新行",
               newer == [] and [r["seq"] for r in older] == [mark],
               f"mark={mark} newer={len(newer)} older={[r['seq'] for r in older]}")

        # 落盘：不只是 file_exists，直接读回文件确认那几行真的写进去了
        on_disk = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                on_disk = fh.read()
        st = hub.stats()
        record("stats 报的 total 与文件真实内容一致（落盘没丢行）",
               st.get("total", 0) >= 3 and st.get("file_exists") is True
               and st.get("last_seq", 0) >= mark
               and "自测-中枢探针A" in on_disk
               and "自测-中枢探针C" in on_disk,
               f"total={st.get('total')} last_seq={st.get('last_seq')} "
               f"file_lines={on_disk.count(chr(10))}")

        # SSE：订阅者必须实时收到，而不是只读历史
        q = hub.subscribe()
        logutil.log("自测-中枢探针D", "WARN")
        got = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                got = q.get_nowait()
            except Exception:
                time.sleep(0.02)
                continue
            if got.get("msg") == "自测-中枢探针D":
                break
            got = None
        hub.unsubscribe(q)
        record("SSE 订阅者实时收到新行",
               got is not None and got.get("level") == "WARN",
               f"got={got.get('msg') if got else None}")
    finally:
        # 拆 Tee：不然后面所有输出都被抄进一个马上要删掉的文件里
        sys.stdout, sys.stderr = saved_out, saved_err
        if hub is not None:
            hub.close()
        for p in (path, path + ".1"):
            try:
                os.path.exists(p) and os.unlink(p)
            except Exception:
                pass


class _StubAuth:
    """只提供 supervisor 2 级重试会碰到的两个方法。"""

    def __init__(self, tag="a"):
        self.tag = tag
        self.refreshes = 0

    def refresh_mstoken(self):
        self.refreshes += 1
        return "ms-" + self.tag


def check_reconnect():
    """回归：上游 WS 掉线必须自己爬起来，且三级策略/状态可见。

    用户要求：「掉线要自动重连，先直接重连，不行就换 token 重启，前端要看到状态」。
    这里不连抖音，直接把 _attempt 换成脚本化的假连接，跑真实的看护线程：
      1. 前 direct_retries 次失败 -> 1 级（同一凭据直连，不换 token）
      2. 再失败 -> 2 级（换 token：调用 auth_loader + refresh_mstoken）
      3. 再失败 -> 3 级 needs_login（等 WebUI 登录唤醒，不烧 CPU）
      4. 唤醒后级别回到 1 级；稳定存活会清零失败计数
    """
    from onebot_bridge.upstream import (LEVEL_DIRECT, LEVEL_LOGIN, LEVEL_TOKEN,
                                        STATE_NEEDS_LOGIN, STATE_STOPPED,
                                        UpstreamSupervisor, snapshot_json)

    # 失败 4 次（direct_retries=2 / token_retries=1），之后一次稳定成功
    script = [(False, "WebSocketTimeoutException: ping timeout", 0.0)] * 4

    class Tracing(UpstreamSupervisor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.decisions = []
            self.reloads = 0
            self.attempts = 0
            self.lock = threading.Lock()

        def _decide_level(self, failures):
            lv = super()._decide_level(failures)
            self.decisions.append((failures, lv))
            return lv

        def _reload_credentials(self):
            with self.lock:
                self.reloads += 1
            return super()._reload_credentials()

        def _attempt(self):
            with self.lock:
                i = self.attempts
                self.attempts += 1
            if i < len(script):
                time.sleep(0.05)        # 模拟一次握手耗时
                return script[i]
            # 连上了：真实实现里这件事由 on_open 记账，这里手动补上
            with self._lock:
                self._connects += 1
                self._connected_at = time.time()
                self._state = "connected"
            if i == len(script):
                return (True, "", 999.0)     # 稳定存活（> stable_sec）
            self._stop.set()                 # 最后一次：让看护线程收工
            return (True, "", 0.0)

    cfg = {"douyin_ws": {
        "ping_interval_sec": 0,
        "reconnect_min_sec": 0.05,
        "reconnect_max_sec": 0.1,
        "direct_retries": 2,
        "token_retries": 1,
        "stable_sec": 0.5,
    }}
    auth = _StubAuth("first")
    loader_calls = []

    def loader():
        loader_calls.append(1)
        return _StubAuth("second")

    sup = Tracing(auth, cfg, on_push=lambda raw: None, auth_loader=loader)
    sup.stable_sec = 0.5        # 绕过 max(5.0, ...) 下限，测试跑快一点
    sup.start()
    try:
        # 等到进入 3 级 needs_login（前面 4 次失败跑完）
        deadline = time.time() + 20
        while time.time() < deadline:
            st = sup.status()
            if st["state"] == STATE_NEEDS_LOGIN:
                break
            time.sleep(0.05)
        st = sup.status()
        record("上游 WS 连续失败后进入 3 级 needs_login",
               st["state"] == STATE_NEEDS_LOGIN and st["level"] == LEVEL_LOGIN
               and st["connected"] is False,
               f"state={st['state']} level={st['level']}")

        # 分级阶梯：1,1 -> 2（换 token）-> 3（需登录）
        ladder = [lv for _f, lv in sup.decisions]
        record("重试级别按 1级→2级→3级 递进",
               ladder[:4] == [LEVEL_DIRECT, LEVEL_DIRECT, LEVEL_TOKEN,
                              LEVEL_LOGIN],
               f"阶梯={ladder}")

        record("2 级重试真的换了凭据（auth_loader + msToken）",
               sup.reloads >= 1 and len(loader_calls) >= 1
               and sup.auth.tag == "second"
               and getattr(sup.auth, "refreshes", 0) >= 1,
               f"reloads={sup.reloads} loader={len(loader_calls)} "
               f"tag={sup.auth.tag}")

        # 前端拿到的必须是纯数据（能被 JSON 序列化，不含活对象）
        snap = snapshot_json(st)
        record("上游状态快照是纯 JSON（前端可直接渲染）",
               isinstance(snap, dict) and "state_text" in snap
               and "level_name" in snap and snap["connected"] is False,
               f"state_text={snap['state_text']}")

        # 换 token（模拟扫码登录成功）后必须自己回到 1 级重新连
        sup.request_token_reload("测试：登录态已更新")
        deadline = time.time() + 20
        while time.time() < deadline:
            if sup.attempts > len(script):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        record("3 级状态下被唤醒后会自己重连并回落 1 级",
               sup.attempts > len(script)
               and sup.status()["level"] == LEVEL_DIRECT
               and sup.status()["failures"] <= 1,
               f"attempts={sup.attempts} level={sup.status()['level']} "
               f"failures={sup.status()['failures']}")

        # 稳定存活清零失败计数（不把抖动一路推到要重新登录）
        record("稳定存活后失败计数清零",
               sup.status()["failures"] == 0,
               f"failures={sup.status()['failures']} "
               f"level={sup.status()['level']}")

        record("supervisor 统计到了连接/断开次数",
               sup.status()["connects"] >= 1 and sup.status()["disconnects"] >= 4,
               f"connects={sup.status()['connects']} "
               f"disconnects={sup.status()['disconnects']}")
    finally:
        sup.stop(timeout=5)
    record("stop() 后状态是 stopped 且线程退出",
           sup.status()["state"] == STATE_STOPPED
           and not (sup._thread and sup._thread.is_alive()),
           f"state={sup.status()['state']}")

    # 手动重连：任何级别下都应立刻把级别拉回 1 级并唤醒
    sup2 = Tracing(_StubAuth("x"), cfg, on_push=lambda raw: None,
                   auth_loader=loader)
    sup2.stable_sec = 0.5
    sup2.start()
    try:
        time.sleep(0.4)
        sup2._set(level=LEVEL_LOGIN, failures=99)
        sup2.force_reconnect("测试手动重连")
        time.sleep(0.3)
        st2 = sup2.status()
        record("手动重连把级别和失败计数清回 1 级",
               st2["level"] == LEVEL_DIRECT and st2["failures"] <= 1,
               f"level={st2['level']} failures={st2['failures']}")

        # 回归：面板「换 token 重连」必须真的换凭据。
        # 曾经的 bug：request_token_reload() 只置 _reload_token=True，全项目
        # 没有任何地方读它 —— 按钮名叫换 token，行为却等同普通重连，token
        # 根本没换（面板上 token_reloads 永远是 0）。
        before = sup2.status()["token_reloads"]
        loader_before = sup2.reloads
        sup2.request_token_reload("测试：面板换 token")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if sup2.status()["token_reloads"] > before:
                break
            time.sleep(0.05)
        st3 = sup2.status()
        record("面板换 token 重连真的重读了凭据（不是空按钮）",
               st3["token_reloads"] > before and sup2.reloads > loader_before,
               f"token_reloads {before}->{st3['token_reloads']} "
               f"loader {loader_before}->{sup2.reloads}")

        # 换 token 只应发生一次，不能被后续循环反复触发
        settled = sup2.status()["token_reloads"]
        time.sleep(0.8)
        record("换 token 标志用完即清（不会每轮都换）",
               sup2.status()["token_reloads"] == settled,
               f"settled={settled} now={sup2.status()['token_reloads']}")
    finally:
        sup2.stop(timeout=5)


# ---------------------------------------------------------------- 测试主流程

async def main():
    cfg = {
        "state_file": "/tmp/dybridge_selftest_state.json",
        "send_interval_sec": 0.0,
        "skip_self": True,
        "emit_notices": False,
        "self_id": 0,
        "forward_ws": {"enabled": True, "host": "127.0.0.1", "port": 16700,
                       "path": "/", "access_token": "", "heartbeat_sec": 0},
        "reverse_ws": {"enabled": True, "urls": ["ws://127.0.0.1:16799/ws"],
                       "access_token": "", "reconnect_min_sec": 0.5,
                       "reconnect_max_sec": 2.0},
        "http_api": {"enabled": True, "host": "127.0.0.1", "port": 16701,
                     "path": "/", "access_token": ""},
        "http_post": {"enabled": False, "urls": []},
        "recall_poll": {"enabled": False},
        "profile_prefetch": {"enabled": False},
    }
    im = StubIM()
    idmap = IdMap(cfg["state_file"])
    self_id = build_self_id(0, SELF_UID)
    print(f"self_id = {self_id}（int64 上限 {2**63-1}）", flush=True)

    # 替换上游 recv
    import dy_apis.douyin_recv_msg as recv_mod
    recv_mod.DouyinRecvMsg = StubRecv
    sys.modules["dy_apis.douyin_recv_msg"].DouyinRecvMsg = StubRecv

    # 反向 WS 服务端（本脚本扮演 AstrBot）
    reverse_events = []
    reverse_ready = asyncio.Event()

    async def reverse_handler(ws):
        reverse_ready.set()
        try:
            async for raw in ws:
                reverse_events.append(json.loads(raw))
        except Exception:
            pass

    reverse_server = await websockets.serve(reverse_handler, "127.0.0.1", 16799)

    bridge = Bridge(cfg, im, idmap, self_id)
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(1.5)

    # ------------------------------------------------------------ 1. 正向 WS
    print("\n[1] 正向 WS", flush=True)
    fws = await websockets.connect("ws://127.0.0.1:16700/",
                                   additional_headers={"X-Client-Role": "universal",
                                                       "X-Self-ID": str(self_id)})
    first = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("正向 WS 连上并收到 lifecycle 事件",
           first.get("post_type") == "meta_event" and first.get("self_id") == self_id,
           json.dumps(first, ensure_ascii=False)[:80])

    # 私聊事件
    bridge._on_douyin_message(dm_msg(sender=PEER_UID, conv=f"0:1:{PEER_UID}:{SELF_UID}",
                                     ctype=1, content={"aweType": 700, "text": "你好"}))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("私聊事件字段正确",
           ev.get("message_type") == "private" and ev.get("user_id") == int(PEER_UID)
           and isinstance(ev.get("message"), list)
           and isinstance(ev.get("message_id"), int),
           f"user_id={ev.get('user_id')} mid={ev.get('message_id')}")

    # 群聊事件
    bridge._on_douyin_message(dm_msg(sender=PEER_UID))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("群聊事件字段正确",
           ev.get("message_type") == "group" and ev.get("group_id") == GROUP_INT
           and ev.get("sub_type") == "normal",
           f"group_id={ev.get('group_id')} sub_type={ev.get('sub_type')}")

    # 自己发的消息被过滤
    bridge._on_douyin_message(dm_msg(sender=SELF_UID))
    kept_sid = next_sid()
    bridge._on_douyin_message(dm_msg(sender=PEER_UID, sid=kept_sid))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("自己发的消息不回灌", ev.get("message_id") == kept_sid,
           f"收到 mid={ev.get('message_id')}")

    # 群系统通知被过滤
    bridge._on_douyin_message(dm_msg(mt=1001, content={"aweType": 100140}))
    kept_sid = next_sid()
    bridge._on_douyin_message(dm_msg(sender=PEER_UID, sid=kept_sid))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("群系统通知默认不推", ev.get("message_id") == kept_sid)

    # 撤回事件
    bridge._on_douyin_message(dm_msg(
        mt=40001, content={},
        ext={"s:is_recalled": "true", "s:target_server_message_id":
             "7689468817252566587", "s:recall_uid": PEER_UID, "s:recall_role": "0"}))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    record("撤回 -> group_recall notice",
           ev.get("post_type") == "notice" and ev.get("notice_type") == "group_recall"
           and ev.get("message_id") == 7689468817252566587
           and ev.get("group_id") == GROUP_INT,
           json.dumps(ev, ensure_ascii=False)[:110])

    # 富文本 @ 还原
    idmap.cache_profile("2916383924691644", "均", "S1", "")
    bridge._on_douyin_message(dm_msg(
        sender=PEER_UID,
        content={"aweType": 700, "text": "@均 在吗",
                 "mention_users": [{"uid": 2916383924691644}]}))
    ev = json.loads(await asyncio.wait_for(fws.recv(), 5))
    types = [s["type"] for s in ev.get("message") or []]
    record("@ 还原成 at 段", "at" in types and ev["message"][0]["data"]["qq"] == "2916383924691644",
           f"segments={types}")
    last_mid = ev["message_id"]

    # ------------------------------------------------------------ 2. action
    print("\n[2] action 路由（正向 WS）", flush=True)

    async def call(action, params, timeout=8):
        echo = {"seq": int(time.time() * 1000) % 100000}
        await fws.send(json.dumps({"action": action, "params": params,
                                   "echo": echo}))
        while True:
            got = json.loads(await asyncio.wait_for(fws.recv(), timeout))
            if got.get("echo") == echo:
                return got

    r = await call("send_private_msg", {"user_id": int(PEER_UID),
                                        "message": [{"type": "text",
                                                     "data": {"text": "hi"}}]})
    record("send_private_msg 成功且 echo 保真",
           r.get("status") == "ok" and r.get("retcode") == 0
           and isinstance(r.get("data", {}).get("message_id"), int),
           f"data={r.get('data')}")

    r = await call("send_group_msg", {"group_id": GROUP_INT, "message": "纯文本也行"})
    record("send_group_msg 接受字符串 message 并转数组语义",
           r.get("status") == "ok" and im.sent[-1]["conv"] == GROUP_ID,
           f"conv={im.sent[-1]['conv']} text={im.sent[-1]['content'].get('text')!r}")

    r = await call("send_msg", {"message_type": "group", "group_id": GROUP_INT,
                                "message": [{"type": "at", "data": {"qq": PEER_UID}},
                                            {"type": "text", "data": {"text": " 在"}}]})
    record("send_msg + at 段带 mentioned_users",
           r.get("status") == "ok" and im.sent[-1]["mentioned"] == [int(PEER_UID)],
           f"mentioned={im.sent[-1]['mentioned']}")

    mid = last_mid
    r = await call("delete_msg", {"message_id": mid})
    record("delete_msg 走真实撤回", r.get("status") == "ok"
           and ("recall", GROUP_ID, mid) in [(c[0], c[1], c[2]) for c in im.calls
                                             if c[0] == "recall"],
           f"retcode={r.get('retcode')}")

    r = await call("get_group_info", {"group_id": GROUP_INT})
    record("get_group_info 字段齐全",
           r["data"].get("group_name") == "ai聊天群"
           and r["data"].get("member_count") == 4
           and r["data"].get("owner_id") == 2916383924691644,
           json.dumps(r["data"], ensure_ascii=False)[:100])

    r = await call("get_group_member_list", {"group_id": GROUP_INT})
    roles = [m["role"] for m in r["data"]]
    record("get_group_member_list 有角色且 user_id 是 int",
           r["data"][0]["role"] == "owner" and r["data"][2]["role"] == "admin"
           and all(isinstance(m["user_id"], int) for m in r["data"]),
           f"roles={roles}")

    r = await call("get_group_member_info", {"group_id": GROUP_INT,
                                             "user_id": int(PEER_UID)})
    record("get_group_member_info 带 card/role",
           r["data"]["card"] == "小名" and r["data"]["role"] == "admin",
           json.dumps(r["data"], ensure_ascii=False)[:100])

    r = await call("get_friend_list", {})
    record("get_friend_list 从单聊会话列表取",
           r["data"] and r["data"][0]["user_id"] == int(PEER_UID),
           f"n={len(r['data'])}")

    r = await call("get_group_list", {})
    record("get_group_list 是数组且群号是 int",
           isinstance(r.get("data"), list) and r["data"]
           and isinstance(r["data"][0]["group_id"], int)
           and r["data"][0]["group_name"] == "ai聊天群",
           f"type={type(r.get('data')).__name__} data={r.get('data')}")

    # 空列表必须保持 []，不能被塌成 {}
    im.conversation_list = lambda *a, **k: ([], False, 0)
    r = await call("get_group_list", {})
    record("空列表保持 [] 不塌成 {}",
           isinstance(r.get("data"), list) and r["data"] == [],
           f"data={r.get('data')!r}")
    del im.conversation_list

    r = await call("get_stranger_info", {"user_id": int(PEER_UID)})
    record("get_stranger_info 有昵称", r["data"].get("nickname") == "Febtt",
           f"nickname={r['data'].get('nickname')}")

    r = await call("get_login_info", {})
    record("get_login_info 返回 self_id",
           r["data"].get("user_id") == self_id and r["data"].get("nickname") == ".ccc",
           json.dumps(r["data"], ensure_ascii=False))

    r = await call("get_version_info", {})
    record("get_version_info 声明 v11",
           r["data"].get("protocol_version") == "v11")

    r = await call("get_msg", {"message_id": mid})
    record("get_msg 用本地索引还原", r["data"].get("message_id") == mid
           and r["data"]["message"][0]["type"] == "text",
           json.dumps(r["data"], ensure_ascii=False)[:90])

    # 未实现的能力必须显式 failed，且不能沉默
    for action, params in [("set_group_ban", {"group_id": GROUP_INT,
                                              "user_id": int(PEER_UID),
                                              "duration": 60}),
                           ("send_like", {"user_id": int(PEER_UID)}),
                           ("send_group_forward_msg", {"group_id": GROUP_INT,
                                                       "messages": []}),
                           ("this_action_does_not_exist", {})]:
        r = await call(action, params)
        record(f"{action} -> failed/retcode",
               r.get("status") == "failed" and r.get("retcode", 0) >= 1400,
               f"retcode={r.get('retcode')} msg={str(r.get('msg'))[:40]}")

    # ------------------------------------------------------------ 3. 反向 WS
    print("\n[3] 反向 WS", flush=True)
    ok = False
    try:
        await asyncio.wait_for(reverse_ready.wait(), 8)
        ok = True
    except asyncio.TimeoutError:
        pass
    record("反向 WS 连上（本脚本当服务端）", ok)

    if ok:
        rev_sid = next_sid()
        bridge._on_douyin_message(dm_msg(sender=PEER_UID, sid=rev_sid))
        got = None
        for _ in range(60):
            for e in reverse_events:
                if e.get("post_type") == "message" and e.get("message_id") == rev_sid:
                    got = e
                    break
            if got:
                break
            await asyncio.sleep(0.1)
        record("反向 WS 也收到同一条事件", got is not None and got.get("self_id") == self_id)

    # ------------------------------------------------------------ 4. HTTP API
    print("\n[4] HTTP API", flush=True)

    def http_call(action, params):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:16701/{action}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    r = await asyncio.to_thread(http_call, "get_group_info", {"group_id": GROUP_INT})
    record("HTTP API get_group_info",
           r.get("status") == "ok" and r["data"]["group_name"] == "ai聊天群",
           json.dumps(r.get("data"), ensure_ascii=False)[:80])

    r = await asyncio.to_thread(http_call, "send_group_msg",
                                {"group_id": GROUP_INT, "message": "via http"})
    record("HTTP API send_group_msg",
           r.get("status") == "ok" and im.sent[-1]["content"].get("text") == "via http")

    r = await asyncio.to_thread(http_call, "set_group_kick",
                                {"group_id": GROUP_INT, "user_id": 1})
    record("HTTP API 未实现能力回 failed/1404",
           r.get("status") == "failed" and r.get("retcode") == 1404,
           f"retcode={r.get('retcode')}")

    # ------------------------------------------------------------ 5. 事件构造单测
    print("\n[5] 事件构造单测", flush=True)
    builder = EventBuilder(idmap, self_id, {"1": "昵称1"}, {}, SELF_UID)
    ev = builder.message_event(dm_msg(sender="1", content={"aweType": 700, "text": "t"}))
    record("EventBuilder 昵称走本地缓存（不阻塞）",
           ev["sender"]["nickname"] == "昵称1")
    ev = builder.recall_event(dm_msg(mt=40001, ctype=1,
                                     conv=f"0:1:{PEER_UID}:{SELF_UID}",
                                     ext={"s:target_server_message_id": "123",
                                          "s:recall_uid": "1"}))
    record("EventBuilder 私聊撤回 -> friend_recall",
           ev["notice_type"] == "friend_recall" and ev["message_id"] == 123,
           json.dumps(ev, ensure_ascii=False)[:90])

    # 富文本降级不崩
    segs = cq.dy_content_to_segments(9999, {"weird": True})
    record("未知 message_type 降级为占位文本且有 raw", segs[0]["type"] == "text"
           and "raw" in segs[0]["data"])

    # ------------------------------------------------------------ 6. 分页护栏
    # 用 try/finally 兜底：只要中途抛异常，也必须先放掉 StubRecv 常驻线程，
    # 否则 asyncio.run 收尾会一直等它，真正的 traceback 永远打不出来（假死）。
    try:
        print("\n[6] conversation/list 上限护栏（回归：limit>50 会静默回空）", flush=True)
        check_pagination()
        print("\n[7] 上游 WS 自动重连（掉线自愈 / 三级策略 / 状态可见）", flush=True)
        check_reconnect()
        print("\n[8] 面板日志中枢（回归：漏调 setup 则日志页恒空）", flush=True)
        check_loghub()
    finally:
        # -------------------------------------------------------- 收尾
        StubRecv._stop = True
        try:
            bridge.stop()
            await asyncio.sleep(0.3)
            task.cancel()
            reverse_server.close()
            await reverse_server.wait_closed()
            try:
                await fws.close()
            except Exception:
                pass
        except Exception as e:  # 收尾失败不能掩盖真正的错误
            print(f"  [WARN] 收尾异常: {type(e).__name__}: {e}", flush=True)

    print("\n" + "=" * 58, flush=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过", flush=True)
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  未通过: {n} — {d}", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
