# -*- coding: utf-8 -*-
"""抖音 IM 协议层：收发 / 撤回 / 群成员 / 会话 / 历史 / 资料。

设计约束：
  * 全部方法都是**同步阻塞**的（底层 requests + websocket-client），
    由 bridge.py 用 asyncio.to_thread 丢进线程池调用，绝不在事件循环里直接跑。
  * 不修改 douyin-spider 任何源码；只 import 它已有的 builder / static / dy_apis。
  * 一切「发送」都要先拿会话票（ticket），票有时效，这里按会话缓存并在失败时失效重取。

对抖音协议的实测结论（全部来自真实抓包/回读，不是推断）：
  单聊  conversation_type=1, conversation_id="0:1:<peer>:<me>", short_id 由 create_conversation 返回
  群聊  conversation_type=2, conversation_id=<纯数字群号>,   short_id == 群号
  群成员 cmd 605 /v1/conversation/participants_list
  群信息 cmd 610 /v2/conversation/get_info_list
  历史   cmd 301 /v1/message/get_by_conversation
  撤回   cmd 702 /v1/message/recall
  会话表 cmd 2006 /v1/conversation/list （con_type=1 单聊 / 2 群聊）

撤回的判定：抖音**没有**独立的撤回通知体，撤回表达在 ImMessage.ext 上：
  s:is_recalled == "true"  + s:recall_uid / s:recall_role
历史接口会把已撤回消息的该标记带回来，所以桥用「定期对账历史」补齐撤回事件。
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, "/root/dyonebot/douyin-spider")
if os.path.isdir("/root/dyonebot/douyin-spider"):
    os.chdir("/root/dyonebot/douyin-spider")
sys.path.insert(0, "/root/dyonebot/dy-bot/proto")

import requests  # noqa: E402

from builder.header import HeaderBuilder, HeaderType  # noqa: E402
from builder.proto import ProtoBuilder  # noqa: E402
from dy_apis.douyin_api import DouyinAPI  # noqa: E402
from Im_pb2 import Request as ImRequest  # noqa: E402
from Im_pb2 import Response as ImResponse  # noqa: E402
from static import Live_pb2, Response_pb2  # noqa: E402
from utils.common_util import load_env  # noqa: E402
from utils.dy_util import generate_a_bogus, splice_url  # noqa: E402

requests.packages.urllib3.disable_warnings()

IM_HOST = "https://imapi.douyin.com"
AUTH_ENV_FILE = "/root/dyonebot/douyin-spider/.env"

CONV_ONE_TO_ONE = 1
CONV_GROUP = 2

ROLE_MEMBER = 0
ROLE_OWNER = 1
ROLE_ADMIN = 2
ROLE_LABEL = {ROLE_MEMBER: "member", ROLE_OWNER: "owner", ROLE_ADMIN: "admin"}
# OneBot 的 sender.role 取值
ROLE_ONEBOT = {ROLE_MEMBER: "member", ROLE_OWNER: "owner", ROLE_ADMIN: "admin"}

EXT_RECALLED = "s:is_recalled"
EXT_RECALL_UID = "s:recall_uid"
EXT_RECALL_ROLE = "s:recall_role"
EXT_RECALL_TS = "s:text_recall_timestamp"


class DouyinIMError(RuntimeError):
    pass


def _text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v if v is not None else ""


def _first_url(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("origin_url_list", "url_list", "urlList", "url"):
            got = _first_url(node.get(key))
            if got:
                return got
    if isinstance(node, list):
        for item in node:
            got = _first_url(item)
            if got:
                return got
    return ""


def _parse_content(raw):
    if isinstance(raw, (dict, list)):
        return raw
    text = _text(raw)
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}


class DouyinIM:
    """一个抖音登录态的 IM 门面。线程安全（内部有锁 + 每线程 requests 连接池）。"""

    def __init__(self, auth=None, profile_cache_sec=1800):
        # Never let the caller's cwd or inherited DY_* variables decide which
        # account is loaded.  The bridge is often restarted by a supervisor
        # with a different cwd, while the durable login is this project's .env.
        self.auth = auth or load_env(env_path=AUTH_ENV_FILE)
        self._lock = threading.RLock()
        self._tickets = {}          # key -> {"ticket","short_id","conv_id","ctype","ts"}
        self._ticket_ttl = 240      # 票的保守寿命，实测可用数分钟
        self._profiles = {}         # uid -> {"nickname","avatar","sec_uid","ts"}
        self._profile_ttl = profile_cache_sec
        self._uid = str(self.auth.get_uid())

    # ------------------------------------------------------------ 基础
    @property
    def self_uid(self):
        return self._uid

    def _post(self, cmd, path, fill, timeout=20):
        """发一个 IM protobuf 请求，返回 Im_pb2.Response。"""
        req = ProtoBuilder.build_normal_request(self.auth, cmd)
        env = ImRequest()
        env.ParseFromString(req.SerializeToString())
        fill(env)

        headers = HeaderBuilder().build(HeaderType.PROTOBUF)
        headers.set_header("referer", "https://www.douyin.com/")
        headers.with_bd(path, self.auth)

        params = {"msToken": self.auth.msToken}
        params["a_bogus"] = generate_a_bogus(splice_url(params),
                                             host="www.douyin.com")
        params["verifyFp"] = self.auth.cookie["s_v_web_id"]
        params["fp"] = self.auth.cookie["s_v_web_id"]

        resp = requests.post(IM_HOST + path, params=params, headers=headers.get(),
                             cookies=self.auth.cookie,
                             data=env.SerializeToString(),
                             verify=False, timeout=timeout)
        rp = ImResponse()
        rp.ParseFromString(resp.content)
        return rp

    # ------------------------------------------------------------ 会话票
    def get_ticket(self, conversation_id, conversation_type, short_id=None,
                   force=False):
        """取（并缓存）一个会话的 ticket。

        群聊：conversation_id 就是群号，short_id 也等于群号，走 cmd 610。
        单聊：conversation_id 是 "0:1:peer:me"，short_id 由 609 创建/查询得到。
        """
        key = (str(conversation_id), int(conversation_type))
        now = time.time()
        with self._lock:
            hit = self._tickets.get(key)
            if hit and not force and now - hit["ts"] < self._ticket_ttl:
                return hit

        if int(conversation_type) == CONV_GROUP:
            short_id = int(short_id or conversation_id)
            conv_id = str(conversation_id)
        else:
            if not short_id:
                # 单聊没有短 id 时，先创建/查询会话拿短 id
                peer = self.peer_of(conversation_id)
                if not peer:
                    raise DouyinIMError(f"单聊会话缺少对方 uid: {conversation_id}")
                try:
                    conv_id, short_id, ticket = DouyinAPI.create_conversation(
                        self.auth, int(peer))
                except Exception as err:
                    raise DouyinIMError(
                        f"创建私聊会话失败 peer={peer}: "
                        f"{type(err).__name__}: {err}") from err
                info = {"conv_id": str(conv_id), "short_id": int(short_id),
                        "ticket": ticket, "ctype": CONV_ONE_TO_ONE, "ts": now}
                with self._lock:
                    self._tickets[key] = info
                return info
            conv_id = str(conversation_id)

        def fill(env):
            b = env.body.get_conversation_info_list_v2_body.data
            b.conversation_id = str(conv_id)
            b.conversation_short_id = int(short_id)
            b.conversation_type = int(conversation_type)

        rp = self._post(610, "/v2/conversation/get_info_list", fill)
        if rp.message != "OK":
            raise DouyinIMError(
                f"get_info_list 失败 cmd={rp.cmd} msg={rp.message} "
                f"err={getattr(rp, 'error_desc', '')}")
        page = rp.body.get_conversation_info_list_v2_body
        if not page.conversation_info_list:
            raise DouyinIMError("get_info_list 返回空会话列表")
        conv = page.conversation_info_list[0]
        info = {
            "conv_id": _text(conv.conversation_id) or str(conv_id),
            "short_id": int(conv.conversation_short_id),
            "ticket": _text(conv.ticket),
            "ctype": int(conv.conversation_type),
            "ts": now,
        }
        if not info["ticket"]:
            raise DouyinIMError("会话票为空")
        with self._lock:
            self._tickets[key] = info
        return info

    def invalidate_ticket(self, conversation_id, conversation_type):
        with self._lock:
            self._tickets.pop((str(conversation_id), int(conversation_type)), None)

    @staticmethod
    def peer_of(conversation_id, self_uid=None):
        """从单聊会话 id "0:1:a:b" 里取出「对方」的 uid。"""
        parts = str(conversation_id or "").split(":")
        if len(parts) == 4:
            a, b = parts[2], parts[3]
            if self_uid and str(self_uid) == a:
                return b
            return a
        return ""

    # ------------------------------------------------------------ 发送
    def send(self, conversation_id, conversation_type, message_type, content,
             mentioned_users=None, ref=None, client_message_id=None,
             short_id=None, _retry_ticket=True):
        """发一条 IM 消息。content 可以是 dict（自动紧凑序列化）。

        返回 (ok, server_message_id, detail)。
        """
        info = self.get_ticket(conversation_id, conversation_type, short_id)
        ctype = int(conversation_type)
        itok, idev = DouyinAPI.get_identity_security_token(self.auth)

        encoded = (json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                   if isinstance(content, (dict, list)) else str(content))
        cmsg = str(client_message_id or __import__("uuid").uuid4())

        def fill(env):
            sb = env.body.send_message_body
            sb.conversation_id = str(info["conv_id"])
            sb.conversation_type = ctype
            sb.conversation_short_id = int(info["short_id"])
            sb.ticket = info["ticket"]
            sb.message_type = int(message_type)
            sb.content = encoded
            sb.client_message_id = cmsg
            if mentioned_users:
                sb.mentioned_users.extend(int(u) for u in mentioned_users)
            if ref:
                sb.ref_msg_info.original_message_id = int(ref.get("id") or 0)
                sb.ref_msg_info.original_message_sender = str(
                    ref.get("sender") or "")
                sb.ref_msg_info.original_message_timestamp = int(
                    ref.get("ts") or int(time.time() * 1000))
            exts = [
                ("s:mentioned_users",
                 ",".join(str(u) for u in (mentioned_users or []))),
                ("s:client_message_id", cmsg),
                ("s:stime", f"{int(time.time() * 1000)}.{12345:05d}"),
            ]
            if ctype == CONV_GROUP:
                exts += [("chat_scene", "group"), ("s:chat_tag", "public_group")]
            for k, v in exts:
                e = sb.ext.add()
                e.key, e.value = k, str(v)

        url = "/v1/message/send"
        req = ProtoBuilder.build_normal_request(self.auth, 100)
        env = ImRequest()
        env.ParseFromString(req.SerializeToString())
        fill(env)
        # 身份安全 token 走 protobuf header map（PC IM 新版要求）
        env.headers["identity_security_token"] = json.dumps({"token": itok},
                                                            separators=(",", ":"))
        env.headers["identity_security_device_id"] = idev
        env.headers["identity_security_aid"] = ""

        headers = HeaderBuilder().build(HeaderType.PROTOBUF)
        headers.set_header("referer", "https://www.douyin.com/")
        headers.with_bd(url, self.auth)
        params = {"msToken": self.auth.msToken}
        params["a_bogus"] = generate_a_bogus(splice_url(params),
                                             host="www.douyin.com")
        params["verifyFp"] = self.auth.cookie["s_v_web_id"]
        params["fp"] = self.auth.cookie["s_v_web_id"]

        resp = requests.post(IM_HOST + url, params=params, headers=headers.get(),
                             cookies=self.auth.cookie,
                             data=env.SerializeToString(),
                             verify=False, timeout=25)
        rp = ImResponse()
        rp.ParseFromString(resp.content)
        sid = 0
        try:
            sid = int(rp.body.send_message_body.server_message_id)
        except Exception:
            sid = 0
        ok = rp.message == "OK"
        detail = (f"http={resp.status_code} cmd={rp.cmd} msg={rp.message} "
                  f"err={getattr(rp, 'error_desc', '')}")
        if not ok:
            # 票可能过期：主动取一张新票并自动重发一次。以前这里只失效
            # 缓存、不重发，导致私聊在运行几分钟后永久表现为“不能回复”。
            self.invalidate_ticket(conversation_id, conversation_type)
            if _retry_ticket:
                try:
                    return self.send(
                        conversation_id, conversation_type, message_type,
                        content, mentioned_users=mentioned_users, ref=ref,
                        client_message_id=client_message_id,
                        short_id=info.get("short_id"), _retry_ticket=False,
                    )
                except Exception as err:
                    detail += f"; 重取票重发失败 {type(err).__name__}: {err}"
        return ok, sid, detail

    # ------------------------------------------------------------ 发送快捷方式
    def send_text(self, conversation_id, conversation_type, text,
                  mentioned_users=None, short_id=None):
        content = {"aweType": 700, "type": 0, "richTextInfos": [], "text": text}
        return self.send(conversation_id, conversation_type, 7, content,
                         mentioned_users=mentioned_users, short_id=short_id)

    def send_image(self, conversation_id, conversation_type, image, short_id=None):
        """发图片（type 27）。image 可以是本地路径/bytes/URL。"""
        from dy_apis.douyin_im_media import DouyinIMMedia
        payload = DouyinIMMedia.upload_image(self.auth, image)
        return self.send(conversation_id, conversation_type,
                         DouyinAPI.IM_STORY_PICTURE, payload, short_id=short_id)

    def send_file(self, conversation_id, conversation_type, path, short_id=None):
        from dy_apis.douyin_im_media import DouyinIMMedia
        payload = DouyinIMMedia.upload_file(self.auth, path)
        return self.send(conversation_id, conversation_type,
                         DouyinAPI.IM_FILE, payload, short_id=short_id)

    # ------------------------------------------------------------ 撤回
    def recall(self, conversation_id, conversation_type, server_message_id,
               short_id=None):
        info = self.get_ticket(conversation_id, conversation_type, short_id)

        def fill(env):
            b = env.body.recall_message_body
            b.conversation_id = str(info["conv_id"])
            b.conversation_short_id = int(info["short_id"])
            b.conversation_type = int(conversation_type)
            b.server_message_id = int(server_message_id)

        rp = self._post(702, "/v1/message/recall", fill)
        return rp.message == "OK", f"cmd={rp.cmd} msg={rp.message} err={getattr(rp, 'error_desc', '')}"

    # ------------------------------------------------------------ 历史
    def history(self, conversation_id, conversation_type, short_id=None,
                limit=30, cursor=0, direction=0):
        """读会话历史。返回 ImMessage 列表（原始 pb 对象）。"""
        info = self.get_ticket(conversation_id, conversation_type, short_id)

        def fill(env):
            b = env.body.get_by_conversation_body
            b.conversation_id = str(info["conv_id"])
            b.conversation_type = int(conversation_type)
            b.conversation_short_id = int(info["short_id"])
            b.cursor = int(cursor)
            b.direction = int(direction)
            b.limit = int(limit)

        rp = self._post(301, "/v1/message/get_by_conversation", fill)
        body = rp.body.get_by_conversation_body
        return list(body.messages), int(body.next_cursor), bool(body.has_more)

    # ------------------------------------------------------------ 群成员
    def participants(self, conversation_id, conversation_type=CONV_GROUP,
                     short_id=None, limit=200):
        info = self.get_ticket(conversation_id, conversation_type, short_id)
        out, seen, cursor, pages = [], set(), 0, 0
        while True:
            def fill(env, _c=cursor):
                b = env.body.conversation_participants_body
                b.conversation_id = str(info["conv_id"])
                b.conversation_short_id = int(info["short_id"])
                b.conversation_type = int(conversation_type)
                b.cursor = int(_c)
                b.limit = int(limit)

            rp = self._post(605, "/v1/conversation/participants_list", fill)
            if rp.message != "OK":
                raise DouyinIMError(
                    f"participants_list 失败 cmd={rp.cmd} msg={rp.message} "
                    f"err={getattr(rp, 'error_desc', '')}")
            page = rp.body.conversation_participants_body.participants_page
            pages += 1
            for p in page.participants:
                uid = str(p.user_id)
                if uid in seen:
                    continue
                seen.add(uid)
                out.append({
                    "user_id": uid,
                    "sec_uid": _text(p.sec_uid),
                    "alias": _text(p.alias),
                    "role": int(p.role),
                    "role_name": ROLE_LABEL.get(int(p.role), f"role_{p.role}"),
                    "blocked": int(p.blocked),
                })
            nxt = int(page.cursor)
            if not page.has_more or nxt in (0, cursor) or pages > 50:
                break
            cursor = nxt
        return out

    # ------------------------------------------------------------ 会话信息
    def conversation_info(self, conversation_id, conversation_type=CONV_GROUP,
                          short_id=None):
        """群/会话信息：名字、头像、人数、群主。"""
        info = self.get_ticket(conversation_id, conversation_type, short_id)

        def fill(env):
            b = env.body.get_conversation_info_list_v2_body.data
            b.conversation_id = str(info["conv_id"])
            b.conversation_short_id = int(info["short_id"])
            b.conversation_type = int(conversation_type)

        rp = self._post(610, "/v2/conversation/get_info_list", fill)
        if rp.message != "OK":
            raise DouyinIMError(f"get_info_list 失败 {rp.message} {getattr(rp, 'error_desc', '')}")
        page = rp.body.get_conversation_info_list_v2_body
        if not page.conversation_info_list:
            raise DouyinIMError("get_info_list 空列表")
        c = page.conversation_info_list[0]
        core = c.core_info
        return {
            "conversation_id": _text(c.conversation_id),
            "short_id": str(c.conversation_short_id),
            "type": int(c.conversation_type),
            "ticket": _text(c.ticket),
            "name": _text(core.name),
            "icon": _text(core.icon),
            "owner": str(core.owner or ""),
            "sec_owner": _text(core.sec_owner),
            "participants_count": int(c.participants_count or 0),
            "online_member_count": int(c.online_member_count or 0),
        }

    # ------------------------------------------------------------ 会话列表
    # 实测：limit > 50 时抖音【不报错】，而是 message="OK" 却回一个空列表
    # （status_code=0、body 为空），静默骗人。所以这里硬夹到 50，只打一发。
    #
    # 不做翻页：这个接口只服务枚举类能力（群列表/好友列表/昵称预取），
    # 真实消息收发走 WS 推送，跟它无关。会话几百上千条时每次全量拉本来就
    # 不现实，取最近 50 条足够用。
    CONVERSATION_PAGE_MAX = 50

    def conversation_list(self, con_type, limit=50, cursor=0, with_cold=1):
        """con_type: 1=单聊 2=群聊。返回 (会话 dict 列表, has_more, total)。

        实测：con_type 只接受 1 或 2；0/3/8 会报 StatusCodeRemoteNetError。
        cursor 是【偏移量】（实测 0->前5条, 1->去掉第1条, 5->空），不是 token。

        limit 的硬边界卡在 50（2026-09 真机实测，单聊/群聊一致）：
            limit=48/49/50 -> 正常回页；limit=51 -> n=0；100/1000/2^31-1 全 n=0。
        回空时 msg="OK"、error_desc 为空、has_more=False、total=0，即服务器收下请求
        但当成「没有任何会话」，不会报错，所以调用方无法靠返回值区分真假空列表。
        已排除整数溢出（proto 里 limit 是 int32，2^31 才在本地报 ValueError）、
        flag 组合（with_cold/sort_type 0~3 在 limit=100 下全为空）等旁路，无解。

        这里把 limit 夹到 <=50 并只请求一发；传更大值也不会多拿，只是避免回空。
        """
        take = max(1, min(int(limit), self.CONVERSATION_PAGE_MAX))

        def fill(env):
            b = env.body.get_user_conversation_list_body
            b.sort_type = 0
            b.cursor = int(cursor)
            b.con_type = int(con_type)
            b.limit = int(take)
            b.with_cold = int(with_cold)

        rp = self._post(2006, "/v1/conversation/list", fill)
        if rp.message != "OK":
            raise DouyinIMError(
                f"conversation/list 失败 {rp.message} "
                f"{getattr(rp, 'error_desc', '')}")
        body = rp.body.get_user_conversation_list_body
        has_more = bool(body.has_more)
        total = int(body.total or 0)

        out, seen = [], set()
        for c in body.conversations:
            cid = _text(c.conversation_id)
            if cid in seen:
                continue
            seen.add(cid)
            core = c.core_info
            out.append({
                "conversation_id": cid,
                "conversation_short_id": str(c.conversation_short_id),
                "type": int(c.conversation_type),
                "name": _text(core.name),
                "icon": _text(core.icon),
                "owner": str(core.owner or ""),
                "participant_count": int(c.participant_count or 0),
                "unread_count": int(c.unread_count or 0),
                "ticket": _text(c.ticket),
                "peer": self.peer_of(cid, self._uid)
                        if int(c.conversation_type) == CONV_ONE_TO_ONE else "",
            })
        return out, has_more, total or len(out)

    # ------------------------------------------------------------ 用户资料
    def profile(self, uid=None, sec_uid=None, use_cache=True):
        """取昵称/头像。优先用 sec_uid 走主站公开接口。

        重要（真机实测）：主站 get_user_info 只认 sec_uid，**只给 uid 必然回空**。
            profile(uid="2916383924691644")                       -> {}
            profile(uid=..., sec_uid="MS4wLjABAAAA...")           -> nickname/avatar 齐
        所以没传 sec_uid 时会先查本进程缓存里有没有该 uid 的 sec_uid；没有就
        直接返回 {}，不浪费一次注定落空的请求。调用方该做的是先把 sec_uid 灌进来
        （群成员列表 participants() 里就带 sec_uid，推送帧里也有 sender_sec_uid）。
        """
        uid = str(uid or "")
        key = sec_uid or uid
        if not key:
            return {}
        now = time.time()
        with self._lock:
            hit = self._profiles.get(key)
            if hit and use_cache and now - hit["ts"] < self._profile_ttl:
                return hit

        sec = sec_uid
        if not sec and uid:
            with self._lock:
                sec = (self._profiles.get(uid) or {}).get("sec_uid", "")
        if not sec:
            return {}

        try:
            data = DouyinAPI.get_user_info(
                self.auth, "https://www.douyin.com/user/" + sec)
        except Exception:
            return {}
        u = (data or {}).get("user") or {}
        if not u:
            return {}
        avatar = ""
        thumb = u.get("avatar_thumb")
        if isinstance(thumb, dict):
            urls = thumb.get("url_list") or []
            avatar = urls[0] if urls else ""
        info = {
            "user_id": str(u.get("uid") or uid or ""),
            "sec_uid": sec,
            "nickname": u.get("nickname") or "",
            # 主站用户资料里的 unique_id 就是用户可见的「抖音号」；
            # 兼容少数返回 short_id / handle 命名的响应。
            "unique_id": (u.get("unique_id") or u.get("short_id")
                          or u.get("handle") or ""),
            "avatar": avatar,
            "ts": now,
        }
        with self._lock:
            self._profiles[key] = info
            if info["user_id"]:
                self._profiles[info["user_id"]] = info
        return info


# ---------------------------------------------------------------- 推送帧解析

def parse_push(raw):
    """解析 frontier-im 的推送帧。

    返回 (kind, payload)：
      ("message", {"conversation_id","conversation_type","message_type",
                   "sender","sender_sec_uid","server_message_id",
                   "short_id","index","content","ext","create_time"})
      ("text", <解码后的 json/str>)  —— payloadType != 'pb'
      ("other", {"oneof":..., "cmd":...})
    """
    frame = Live_pb2.PushFrame()
    frame.ParseFromString(raw)
    if frame.payloadType != "pb":
        body = frame.payload
        try:
            body = body.decode("utf-8")
            body = json.loads(body)
        except Exception:
            body = _text(frame.payload)
        return "text", body

    # 用 Im_pb2.Response 而不是项目的 Response_pb2：两者的 1..5 号字段完全
    # 兼容，但 Im.proto 的 ImMessage 多出 9=ext / 10=create_time / 14=sender_sec_uid。
    # 撤回标记就藏在 ext 里，用项目那份会把它整段丢掉。
    resp = ImResponse()
    resp.ParseFromString(frame.payload)
    which = resp.body.WhichOneof("body")
    if which != "new_message_notify":
        return "other", {"oneof": which, "cmd": int(resp.cmd)}
    m = resp.body.new_message_notify.message
    ext = {e.key: e.value for e in m.ext}
    return "message", {
        "conversation_id": _text(m.conversation_id),
        "conversation_type": int(m.conversation_type),
        "message_type": int(m.message_type),
        "sender": str(m.sender),
        "sender_sec_uid": _text(m.sender_sec_uid),
        "server_message_id": int(m.server_message_id),
        "short_id": int(m.conversation_short_id),
        "index": int(m.index_in_conversation),
        "content": _parse_content(m.content),
        "ext": ext,
        "create_time": int(m.create_time or 0),
    }


def im_message_to_dict(m):
    """把 ImMessage（历史接口返回的 pb 对象）转成和推送一样的字典。"""
    return {
        "conversation_id": _text(m.conversation_id),
        "conversation_type": int(m.conversation_type),
        "message_type": int(m.message_type),
        "sender": str(m.sender),
        "sender_sec_uid": _text(m.sender_sec_uid),
        "server_message_id": int(m.server_message_id),
        "short_id": int(m.conversation_short_id),
        "index": int(m.index_in_conversation),
        "content": _parse_content(m.content),
        "ext": {e.key: e.value for e in m.ext},
        "create_time": int(m.create_time or 0),
    }


def is_recalled(msg):
    """判断一条消息是否已被撤回（读 ext，不猜 message_type）。"""
    ext = (msg or {}).get("ext") or {}
    return str(ext.get(EXT_RECALLED, "")).lower() == "true"
