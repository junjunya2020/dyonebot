# -*- coding: utf-8 -*-
"""抖音 IM 补充接口客户端（仓库外部，不改动 douyin-spider 源码）。

douyin-spider 自带的 DouyinAPI 只覆盖了「私信收发」。这里补齐群管理
需要的那几个接口，字段号全部来自线上 IM SDK 的 im_proto 描述符：

    cmd 605   POST /v1/conversation/participants_list   群成员列表
    cmd 702   POST /v1/message/recall                   撤回消息
    cmd 2015  POST /v1/profile/get_info                 取昵称/头像

实现要点：信封复用项目的 ProtoBuilder.build_normal_request()（它封装了
全部头部字段与鉴权细节），然后用本目录 proto/Im_pb2.py 里字段号一致的
Request 反解 → 只填 body → 重新序列化。这样不需要给项目的 Request.proto
oneof 增加字段，也就完全不用碰项目源码。
"""
import json
import os
import sys

sys.path.insert(0, "/root/dyonebot/douyin-spider")
os.chdir("/root/dyonebot/douyin-spider")

import requests

from builder.header import HeaderBuilder, HeaderType
from builder.proto import ProtoBuilder
from dy_apis.douyin_api import DouyinAPI
from static import Response_pb2
from utils.common_util import load_env
from utils.dy_util import generate_a_bogus, splice_url

sys.path.insert(0, "/root/dyonebot/dy-bot/proto")
from Im_pb2 import Request as ImRequest  # noqa: E402

requests.packages.urllib3.disable_warnings()

IM_HOST = "https://imapi.douyin.com"

# 群聊 = 2，单聊 = 1（线上 ConversationType）
CONV_GROUP = 2
CONV_ONE_TO_ONE = 1

# Participant.role 的线上取值（实测：群主=1，普通成员=0；管理员=2）
ROLE_MEMBER = 0
ROLE_OWNER = 1
ROLE_ADMIN = 2
ROLE_LABEL = {ROLE_MEMBER: "member", ROLE_OWNER: "owner", ROLE_ADMIN: "admin"}

# MessageBody.ext 里的撤回标记（web 端 IM SDK 常量）
EXT_RECALL_ROLE = "s:recall_role"
EXT_RECALL_UID = "s:recall_uid"
EXT_RECALL_TS = "s:text_recall_timestamp"
# 群头像 / 群名等扩展
EXT_GROUP_NAME = "a:group_name"
EXT_AB_AVATAR = "a:ab_avatar"


def _im_post(auth, cmd, path, fill_body, timeout=20):
    """发一个 IM protobuf 请求。

    fill_body(ImRequest) 负责往 body 上填字段。返回解析好的
    Response_pb2.Response（信封与项目里那份字段号一致）。
    """
    req = ProtoBuilder.build_normal_request(auth, cmd)
    env = ImRequest()
    env.ParseFromString(req.SerializeToString())
    fill_body(env)

    headers = HeaderBuilder().build(HeaderType.PROTOBUF)
    headers.set_header("referer", "https://www.douyin.com/")
    headers.with_bd(path, auth)

    params = {"msToken": auth.msToken}
    params["a_bogus"] = generate_a_bogus(splice_url(params), host="www.douyin.com")
    params["verifyFp"] = auth.cookie["s_v_web_id"]
    params["fp"] = auth.cookie["s_v_web_id"]

    resp = requests.post(IM_HOST + path, params=params, headers=headers.get(),
                         cookies=auth.cookie, data=env.SerializeToString(),
                         verify=False, timeout=timeout)
    rp = Response_pb2.Response()
    rp.ParseFromString(resp.content)
    return resp.status_code, rp


def _text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


# --------------------------------------------------------------- 群成员列表

def get_participants(auth, conversation_id, conversation_short_id,
                     conversation_type=CONV_GROUP, cursor=0, limit=200):
    """cmd 605 群成员列表，自动翻页。返回 (members, page_info)。

    members: [{"user_id", "sec_uid", "alias", "role", "role_name", "blocked"}]
    """
    members = []
    seen = set()
    pages = 0
    cursor = int(cursor or 0)

    while True:
        def fill(env, _c=cursor):
            b = env.body.conversation_participants_body
            b.conversation_id = str(conversation_id)
            b.conversation_short_id = int(conversation_short_id)
            b.conversation_type = int(conversation_type)
            b.cursor = int(_c)
            b.limit = int(limit)

        http, rp = _im_post(auth, 605, "/v1/conversation/participants_list", fill)
        if rp.message != "OK" and rp.error_desc:
            raise RuntimeError(f"participants_list 失败 http={http} "
                               f"cmd={rp.cmd} msg={rp.message} err={rp.error_desc}")

        # 反解真实响应体：项目 Response.proto 没有 605 分支，所以直接用
        # blackboxprotobuf 会丢类型；这里改从原始字节走 im_proto 定义。
        page = _parse_participants(rp)
        pages += 1
        for p in page["participants"]:
            if p["user_id"] in seen:
                continue
            seen.add(p["user_id"])
            members.append(p)
        if not page["has_more"] or page["cursor"] in (0, cursor) or pages > 50:
            return members, page
        cursor = page["cursor"]


def _parse_participants(rp):
    """从 Response 的 body 里取出 605 的 ParticipantsPage。

    项目的 Response_pb2.ResponseBody 没有 605 分支，所以这里对
    body 做一次「手动定位」：直接在原始 Response 字节里找到 6 号字段
    （body），再在 body 里找 1 号字段（605 的 body）→ 1 号（participants_page）。
    用官方 protobuf 解析器逐层剥离，避免手写 varint。
    """
    from google.protobuf.internal.decoder import _DecodeVarint
    from google.protobuf.internal.encoder import _VarintBytes

    raw = rp.SerializeToString()
    body = _field(raw, 6, wire=2)        # Response.body -> ResponseBody
    if body is None:
        return {"participants": [], "has_more": False, "cursor": 0}
    # ResponseBody 用 oneof，字段号就是 cmd 号：605 -> conversation_participants_body
    inner = _field(body, 605, wire=2)
    if inner is None:
        return {"participants": [], "has_more": False, "cursor": 0}
    page_bytes = _field(inner, 1, wire=2)  # ParticipantsPage
    if page_bytes is None:
        return {"participants": [], "has_more": False, "cursor": 0}

    sys.path.insert(0, "/root/dyonebot/dy-bot/proto")
    from Im_pb2 import ParticipantsPage
    page = ParticipantsPage()
    page.ParseFromString(page_bytes)
    out = []
    for p in page.participants:
        out.append({
            "user_id": str(p.user_id),
            "sec_uid": p.sec_uid,
            "alias": p.alias,
            "role": int(p.role),
            "role_name": ROLE_LABEL.get(int(p.role), f"role_{p.role}"),
            "blocked": int(p.blocked),
        })
    return {"participants": out, "has_more": bool(page.has_more),
            "cursor": int(page.cursor)}


def _field(raw, target, wire=2):
    """在一个 protobuf 字节串里按字段号取第一个匹配字段的【负载】。

    只做一层扫描，不递归；目的是在项目 proto 缺少分支时把内层
    message 的字节切出来。
    """
    from google.protobuf.internal.decoder import _DecodeVarint
    pos = 0
    n = len(raw)
    while pos < n:
        try:
            tag, pos = _DecodeVarint(raw, pos)
        except Exception:
            return None
        fnum, fwire = tag >> 3, tag & 7
        if fwire == 0:
            _, pos = _DecodeVarint(raw, pos)
            continue
        if fwire == 2:
            ln, pos = _DecodeVarint(raw, pos)
            payload = raw[pos:pos + ln]
            pos += ln
            if fnum == target and fwire == wire:
                return payload
            continue
        if fwire == 5:
            pos += 4
            continue
        if fwire == 1:
            pos += 8
            continue
        return None
    return None


# --------------------------------------------------------------- 撤回

def recall(auth, conversation_id, conversation_short_id, server_message_id,
           conversation_type=CONV_GROUP):
    """cmd 702 撤回消息。返回 (ok, message)。"""
    def fill(env):
        b = env.body.recall_message_body
        b.conversation_id = str(conversation_id)
        b.conversation_short_id = int(conversation_short_id)
        b.conversation_type = int(conversation_type)
        b.server_message_id = int(server_message_id)

    http, rp = _im_post(auth, 702, "/v1/message/recall", fill)
    ok = (rp.message == "OK")
    return ok, f"http={http} cmd={rp.cmd} msg={rp.message} err={rp.error_desc}"


# --------------------------------------------------------------- 用户资料

_profile_cache = {}


def get_profile(auth, sec_uid):
    """按 sec_uid 取昵称/头像。

    注：IM 的 cmd 2015 `/v1/profile/get_info` 在 web 端【没有实现】——
    实测返回的是 JSON `{"BaseResp":{},"Header":{},"Method":0,"Service":0}`，
    不是 protobuf，也不是资料。因此这里改用主站公开接口
    `/aweme/v1/web/user/profile/other/`（项目自带的 DouyinAPI.get_user_info）。
    participants_list 返回的 Participant 自带 sec_uid，正好能直接喂进来。
    """
    if not sec_uid:
        return None
    if sec_uid in _profile_cache:
        return _profile_cache[sec_uid]
    try:
        data = DouyinAPI.get_user_info(
            auth, "https://www.douyin.com/user/" + sec_uid)
    except Exception:
        return None
    u = (data or {}).get("user") or {}
    if not u:
        return None
    info = {
        "user_id": str(u.get("uid") or ""),
        "sec_uid": sec_uid,
        "nickname": u.get("nickname", ""),
        "avatar": (u.get("avatar_thumb") or {}).get("url_list", [""])[0]
                  if isinstance(u.get("avatar_thumb"), dict) else "",
    }
    _profile_cache[sec_uid] = info
    return info


# --------------------------------------------------------------- 群信息

def get_conversation_info(auth, conversation_id, conversation_short_id,
                          conversation_type=CONV_GROUP):
    """cmd 610 取会话信息（含 ticket、群名、群主、人数）。"""
    url = IM_HOST + "/v2/conversation/get_info_list"
    req = ProtoBuilder.build_normal_request(auth, 610)
    body = req.body.get_conversation_info_list_v2_body.data
    body.conversation_id = str(conversation_id)
    body.conversation_short_id = int(conversation_short_id)
    body.conversation_type = int(conversation_type)

    headers = HeaderBuilder().build(HeaderType.PROTOBUF)
    headers.set_header("referer", "https://www.douyin.com/")
    headers.with_bd("/v2/conversation/get_info_list", auth)

    params = {"msToken": auth.msToken}
    params["a_bogus"] = generate_a_bogus(splice_url(params), host="www.douyin.com")
    params["verifyFp"] = auth.cookie["s_v_web_id"]
    params["fp"] = auth.cookie["s_v_web_id"]

    resp = requests.post(url, params=params, headers=headers.get(),
                         cookies=auth.cookie, data=req.SerializeToString(),
                         verify=False, timeout=20)
    rp = Response_pb2.Response()
    rp.ParseFromString(resp.content)
    if rp.message != "OK":
        return None

    import blackboxprotobuf
    data, _ = blackboxprotobuf.decode_message(resp.content)
    try:
        conv = data["6"]["610"]["1"]
    except Exception:
        return None

    def t(node):
        return node.decode("utf-8") if isinstance(node, bytes) else node

    core = conv.get("50") or {}
    page = conv.get("6") or {}
    # ConversationCoreInfo 字段号（线上 im_proto，已核对）：
    #   5=name 7=icon 12=owner 13=sec_owner
    return {
        "conversation_id": t(conv.get("1")),
        "short_id": str(t(conv.get("2"))),
        "type": int(t(conv.get("3")) or 0),
        "ticket": t(conv.get("4")),
        "name": t(core.get("5")) or "",
        "icon": t(core.get("7")) or "",
        "owner": str(t(core.get("12")) or ""),
        "sec_owner": t(core.get("13")) or "",
        "participants_count": int(t(conv.get("7")) or 0),
        "online_member_count": int(t(conv.get("30")) or 0),
        "first_page_participants": page.get("1") or [],
        "raw": conv,
    }


def load_auth():
    return load_env()


if __name__ == "__main__":
    # 自检：打印群成员 + 群信息
    mode = sys.argv[1] if len(sys.argv) > 1 else "members"
    auth = load_auth()
    gid = sys.argv[2] if len(sys.argv) > 2 else "7689446943537234469"

    if mode == "members":
        info = get_conversation_info(auth, gid, gid)
        print("会话:", json.dumps({k: v for k, v in (info or {}).items()
                                   if k not in ("raw", "first_page_participants")},
                                  ensure_ascii=False), flush=True)
        members, page = get_participants(auth, gid, gid)
        print(f"成员数 {len(members)} has_more={page['has_more']}", flush=True)
        for m in members:
            prof = get_profile(auth, m["sec_uid"]) or {}
            print(f"  {m['user_id']:20s} {m['role_name']:7s} "
                  f"{prof.get('nickname', '?')}", flush=True)
    elif mode == "recall":
        # 用法: recall <conv_id> <server_message_id>
        mid = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        from dyim_client import recall
        print(recall(auth, gid, gid, mid), flush=True)
    elif mode == "profile":
        print(json.dumps(get_profile(auth, gid), ensure_ascii=False))
