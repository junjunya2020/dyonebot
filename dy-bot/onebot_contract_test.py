# -*- coding: utf-8 -*-
"""AstrBot OneBot v11 契约自测（桥对接前的回归测试）。

目的：在你写桥的过程中，随时用它验证「AstrBot 侧是否按 OneBot v11 预期工作」。
每个用例都是对真实 AstrBot 的一次端到端探测，不是纸面断言。

用法：
  /root/dyonebot/douyin-spider/.venv/bin/python onebot_contract_test.py

前置：AstrBot 已配置 OneBot v11 平台，反向 WS 监听 6199。
"""
import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:6199/ws"
TOKEN = ""                       # 若 AstrBot 配了 ws_reverse_token 就填这里
SELF_ID = 7000000000000000001    # 桥自编的机器人账号（纯数字、int64 内）
PEER_UID = 2692024583072670      # 抖音对方 uid（真实量级 2.6e15）
GROUP_ID = 7689446943537234469   # 抖音群号

BASE_HEADERS = {"X-Self-ID": str(SELF_ID), "User-Agent": "OneBot/11 dyonebot"}


def headers(role="universal", token=TOKEN):
    h = {**BASE_HEADERS, "X-Client-Role": role}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


async def collect(ws, seconds=8):
    """收 AstrBot 回调，回 ok。返回 [(action, params)]。"""
    calls = []
    try:
        while True:
            p = json.loads(await asyncio.wait_for(ws.recv(), timeout=seconds))
            if "action" in p:
                calls.append((p["action"], p.get("params") or {}))
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0,
                    "data": {"message_id": 1}, "echo": p.get("echo"),
                }))
    except asyncio.TimeoutError:
        pass
    return calls


def private_event(text="/help", msg_id=10001):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "time": 1790343296, "self_id": SELF_ID, "user_id": PEER_UID,
        "message_id": msg_id, "raw_message": text, "font": 0,
        "sender": {"user_id": PEER_UID, "nickname": "抖音用户"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


def group_event(text="/help", msg_id=10002, segs=None):
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "time": 1790343296, "self_id": SELF_ID, "group_id": GROUP_ID,
        "user_id": PEER_UID, "message_id": msg_id, "raw_message": text, "font": 0,
        "sender": {"user_id": PEER_UID, "nickname": "抖音群友", "role": "member"},
        "message": segs or [{"type": "text", "data": {"text": text}}],
    }


RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)


async def t_handshake_ok():
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        record("握手成功（universal + X-Self-ID）", True)
        return True


async def t_role_required():
    try:
        async with websockets.connect(URL, additional_headers=dict(BASE_HEADERS)):
            record("缺 X-Client-Role 被拒", False, "竟然连上了")
    except Exception as e:
        record("缺 X-Client-Role 被拒", "400" in str(e), str(e)[:60])


async def t_token():
    if not TOKEN:
        print("  [SKIP] token 用例（未启用 token）", flush=True)
        return
    try:
        async with websockets.connect(URL, additional_headers=headers(token="WRONG")):
            record("错误 token 被拒", False, "竟然连上了")
    except Exception as e:
        record("错误 token 被拒", "403" in str(e), str(e)[:60])
    async with websockets.connect(URL, additional_headers=headers(token=TOKEN)):
        record("正确 token 通过", True)


async def t_private_loop():
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        await ws.send(json.dumps(private_event()))
        calls = await collect(ws)
        actions = [a for a, _ in calls]
        ok = any(a in ("send_private_msg", "send_msg") for a in actions)
        record("私聊闭环：event -> AstrBot -> 发消息回调", ok, f"actions={actions}")


async def t_group_loop():
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        await ws.send(json.dumps(group_event()))
        calls = await collect(ws)
        actions = [a for a, _ in calls]
        ok = "send_group_msg" in actions
        record("群聊闭环：event -> AstrBot -> send_group_msg", ok, f"actions={actions}")


async def t_ids_int64():
    """UID 必须以 JSON number 传，且不超 int64；AstrBot 会 str() 后按数字串处理。"""
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        await ws.send(json.dumps(private_event()))
        calls = await collect(ws)
        params = calls[0][1] if calls else {}
        uid = params.get("user_id")
        ok = isinstance(uid, int) and uid == PEER_UID
        record("UID 以 JSON number 往返且保真", ok, f"回传 user_id={uid!r} type={type(uid).__name__}")


async def t_unsupported_action_safe():
    """@ 段与 reply 段会触发 get_group_member_info / get_msg，桥不实现时应回 failed。

    断言的是「不崩溃 + 后续仍能工作」，而不是「一定回消息」——后者取决于
    是否配了大模型，属于环境依赖，不该写进契约测试。
    验证方式：先发一条带 reply+at 的消息（触发未实现 action，全部回 failed），
    紧接着再发一条 /help（内置命令，无需大模型必回），看第二条能否被正常处理。
    """
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        segs = [
            {"type": "reply", "data": {"id": "9999"}},
            {"type": "at", "data": {"qq": str(SELF_ID)}},
            {"type": "text", "data": {"text": "你好"}},
        ]
        await ws.send(json.dumps(group_event(msg_id=10003, segs=segs)))

        calls = []
        try:
            # 第一波：桥对未实现 action 一律回 failed
            while True:
                p = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
                if "action" in p:
                    calls.append(p["action"])
                    await ws.send(json.dumps({
                        "status": "failed", "retcode": 1404,
                        "msg": "not implemented", "echo": p.get("echo"),
                    }))
        except asyncio.TimeoutError:
            pass

        # 第二波：发内置命令，验证链路在 failed 之后依然活着
        await ws.send(json.dumps(group_event(text="/help", msg_id=10004)))
        alive = False
        try:
            while True:
                p = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if "action" in p:
                    calls.append(p["action"])
                    if p["action"] == "send_group_msg":
                        alive = True
                    await ws.send(json.dumps({
                        "status": "ok", "retcode": 0, "data": {},
                        "echo": p.get("echo"),
                    }))
        except asyncio.TimeoutError:
            pass

        hit_unsupported = any(a in ("get_msg", "get_group_member_info") for a in calls)
        record("桥回 failed 后 AstrBot 未崩溃且链路仍可用",
               alive and hit_unsupported,
               f"触发未实现={hit_unsupported} 后续仍回消息={alive} actions={calls}")


async def t_string_format_rejected():
    """message 传字符串（非数组）会被 AstrBot 判为非法并回错误提示。"""
    async with websockets.connect(URL, additional_headers=headers()) as ws:
        ev = private_event(msg_id=10009)
        ev["message"] = "hi"          # 故意用字符串
        await ws.send(json.dumps(ev))
        calls = await collect(ws, seconds=8)
        ok = len(calls) > 0           # AstrBot 会回一条错误提示
        record("message 必须是数组（字符串会被拒并提示）", ok,
               f"actions={[a for a,_ in calls]}")


async def main():
    print(f"目标: {URL}\n" + "=" * 58, flush=True)
    tests = [
        ("握手", t_handshake_ok),
        ("角色头", t_role_required),
        ("token", t_token),
        ("私聊", t_private_loop),
        ("群聊", t_group_loop),
        ("ID", t_ids_int64),
        ("容错", t_unsupported_action_safe),
        ("格式", t_string_format_rejected),
    ]
    for label, fn in tests:
        print(f"\n[{label}]", flush=True)
        try:
            await fn()
        except Exception as e:
            record(f"{label} 用例异常", False, f"{type(e).__name__}: {str(e)[:80]}")

    print("\n" + "=" * 58, flush=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} 通过", flush=True)
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  未通过: {n} — {d}", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
