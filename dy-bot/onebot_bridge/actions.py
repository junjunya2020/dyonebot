# -*- coding: utf-8 -*-
"""OneBot v11 action 路由表。

调用方向：OneBot 客户端（AstrBot / NoneBot / NapCat 生态） -> 本桥。

设计原则：
  * **抖音真的有的能力，就真做**（发送、撤回、群成员、群信息、用户资料、好友列表）。
  * **抖音没有的能力，一律回 retcode=1404**，并带一句人话 msg。
    绝不静默不回（对方的 future 会挂到 api_timeout），也绝不假装成功
    （上层会以为消息发出去了）。
  * 所有抖音调用都是同步阻塞的，因此所有 handler 都设计成在
    ``asyncio.to_thread`` 里跑，见 bridge.py。

已实测落地的抖音端点（见 dyim.py 顶部注释）：
    cmd 100  /v1/message/send                发消息
    cmd 301  /v1/message/get_by_conversation 历史（get_msg 靠它 + 本地索引）
    cmd 605  /v1/conversation/participants_list 群成员
    cmd 610  /v2/conversation/get_info_list  会话信息（群名/人数/群主）
    cmd 702  /v1/message/recall              撤回
    cmd 2006 /v1/conversation/list           会话列表（好友/群列表）
    cmd 2015 /v1/profile/get_info            用户昵称头像
"""
import base64
import binascii
import re
import time

from . import cq
from .dyim import (CONV_GROUP, CONV_ONE_TO_ONE, ROLE_ONEBOT, DouyinIMError,
                   ROLE_MEMBER, im_message_to_dict)
from .logutil import log

# ---------------------------------------------------------------- retcode

RET_OK = 0
RET_BAD_PARAM = 1400
RET_BAD_HANDLER = 1401
RET_BAD_PARAMS = 1402
RET_BAD_REQUEST = 1404          # 抖音没有这个能力


def ok(data=None, echo=None):
    # 注意：不能写 `data or {}`——空列表 [] 会被悄悄变成 {}，
    # 而 get_group_list / get_friend_list 这类 action 的 data 必须是数组。
    return {"status": "ok", "retcode": RET_OK,
            "data": {} if data is None else data, "echo": echo}


def error(retcode, msg, echo=None):
    return {"status": "failed", "retcode": retcode, "msg": msg, "echo": echo}


class ActionError(Exception):
    def __init__(self, retcode, msg):
        super().__init__(msg)
        self.retcode = retcode
        self.msg = msg


def _need(params, *keys):
    out = []
    for k in keys:
        v = params.get(k)
        if v in (None, ""):
            raise ActionError(RET_BAD_PARAMS, f"缺少参数 {k}")
        out.append(v)
    return out


def _as_int(value, name="value"):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ActionError(RET_BAD_PARAMS, f"{name} 必须是整数: {value!r}")


# ---------------------------------------------------------------- 消息段 -> 抖音

_HTTP_RE = re.compile(r"^https?://", re.I)


def _decode_media(ref):
    """把 OneBot 的 file 字段（url / 本地路径 / base64:// / file://）变成抖音能吃的输入。

    返回 (payload_for_upload, source_name)。
    """
    ref = str(ref or "").strip()
    if not ref:
        raise ActionError(RET_BAD_PARAMS, "媒体段缺少 file")
    if ref.startswith("base64://"):
        raw = ref[len("base64://"):]
        try:
            data = base64.b64decode(raw)
        except (binascii.Error, ValueError) as err:
            raise ActionError(RET_BAD_PARAMS, f"base64 解码失败: {err}") from err
        return data, "upload.bin"
    if ref.startswith("file://"):
        ref = ref[len("file://"):]
    if _HTTP_RE.match(ref):
        return ref, ref.rsplit("/", 1)[-1].split("?")[0] or "upload.bin"
    return ref, ref.rsplit("/", 1)[-1] or "upload.bin"


def _attach_caption(payload, content):
    """把同一条消息里的文字挂到图片/文件 content 上，而不是丢掉。

    实测依据：抖音图片消息（type 27）的 content 本身就是
    ``{"aweType": 2702, "resource_url": {...}, ...}``，并不带文本字段，
    所以 [text, image] 这种混排如果按原样发，文字会被静默吞掉
    （OneBot 侧看不到任何报错，因为发送本身是成功的）。
    这里把文本作为 ``caption``/``text`` 一起塞回去；抖音不认的字段会被忽略，
    不会让整条发送失败，而能认的客户端就会显示出文字。
    """
    text = ""
    if isinstance(content, dict):
        text = str(content.get("text") or "").strip()
    if not text or not isinstance(payload, dict):
        return payload
    payload.setdefault("caption", text)
    payload.setdefault("text", text)
    return payload


class Actions:
    """把 OneBot action 翻译成抖音 IM 调用。线程安全（无实例状态，全部走 ctx）。"""

    def __init__(self, im, idmap, config, self_id, nicknames=None, media=None):
        self.im = im
        self.idmap = idmap
        self.config = config
        self.self_id = int(self_id)
        self.nicknames = nicknames or {}      # uid -> nickname，get_group_member_info 用
        self.media = media                    # MediaCache，get_image 用
        self._last_send = 0.0

    # ------------------------------------------------------------ 公共助手
    def _throttle(self):
        gap = float((self.config.get("send_interval_sec") or 0))
        if gap <= 0:
            return
        wait = self._last_send + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_send = time.time()

    def _resolve_target(self, params, message_type=None):
        """算出 (conversation_id, conversation_type, onebot_target_id)。"""
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        mtype = message_type or params.get("message_type")
        if mtype == "group" or (mtype is None and group_id not in (None, "")):
            gid = _as_int(group_id if group_id not in (None, "") else user_id, "group_id")
            return str(gid), CONV_GROUP, gid
        if user_id in (None, ""):
            raise ActionError(RET_BAD_PARAMS, "必须提供 user_id 或 group_id")
        uid = _as_int(user_id, "user_id")
        return self._private_conversation(uid), CONV_ONE_TO_ONE, uid

    def _private_conversation(self, peer_uid):
        """单聊的 conversation_id 形如 "0:1:<peer>:<self>"。

        抖音对已存在会话是幂等的，这个串只是给 get_ticket/create_conversation
        用来定位会话；真正的 ticket 在 dyim.get_ticket 里换。
        """
        peer = self.idmap.to_douyin(peer_uid)
        return f"0:1:{peer}:{self.im.self_uid}"

    def _remember(self, message_id, conv_id, ctype, sender=0, text=""):
        if not message_id:
            return
        try:
            self.idmap.remember_message(message_id, conv_id, ctype,
                                        message_id, sender=sender, text=text)
        except Exception:
            pass

    # ------------------------------------------------------------ 发送
    def build_dy_payload(self, message):
        """OneBot message -> (message_type, content, mentioned, extras)。

        图片/文件会在这里同步上传到抖音（阻塞，调用方负责丢线程池）。
        表情包（人脸/收藏表情，type 5）不需要上传：它的 image_id + uri 就是
        CDN 上已有的资源，直接原样重发即可（实测 cmd=100 OK，可回读到同一条）。
        """
        segments = cq.normalize_segments(message)
        dy_type, content, mentioned, extras = cq.segments_to_dy(segments, self.idmap)

        sticker = extras.get("sticker")
        if sticker:
            # 表情消息（type 5）不需要任何上传/审核：收到的 raw content 就是
            # 完整的发送体，原样回发即可（实测 aweType 510 与 507 都能发出，
            # 且回读 aweType/display_name 不变）。
            #
            # 只清掉「每条消息各自独立、不该跟着复制」的展示态字段，
            # 其余字段一律保留 —— 少改字段就少一次被服务端判非法。
            payload = dict(sticker)
            for k in ("message_reply_display_type",
                      "top_desc_receiver_v2", "top_desc_receiver_v3",
                      "top_desc_sender_v2", "top_desc_sender_v3",
                      "top_desc_uid_match", "top_desc_scene_type",
                      "ref_msg_info", "danmaku_id"):
                payload.pop(k, None)
            _attach_caption(payload, content)
            return 5, payload, mentioned, extras

        images = [u for u in extras.get("images") or [] if u]
        files = [u for u in extras.get("files") or [] if u]
        if len(images) > 1:
            raise ActionError(RET_BAD_PARAMS, "抖音单条消息只支持一张图片")
        if images:
            from dy_apis.douyin_im_media import DouyinIMMedia
            payload, _name = _decode_media(images[0])
            try:
                uploaded = DouyinIMMedia.upload_image(self.im.auth, payload)
            except Exception as err:
                raise ActionError(RET_BAD_REQUEST,
                                  f"图片上传失败: {type(err).__name__}: {err}") from err
            _attach_caption(uploaded, content)
            return 27, uploaded, mentioned, extras
        if files:
            from dy_apis.douyin_im_media import DouyinIMMedia
            payload, _name = _decode_media(files[0])
            try:
                uploaded = DouyinIMMedia.upload_file(self.im.auth, payload)
            except Exception as err:
                raise ActionError(RET_BAD_REQUEST,
                                  f"文件上传失败: {type(err).__name__}: {err}") from err
            _attach_caption(uploaded, content)
            return 6, uploaded, mentioned, extras
        return dy_type, content, mentioned, extras

    def _do_send(self, params, message_type=None):
        message = params.get("message")
        if message is None:
            raise ActionError(RET_BAD_PARAMS, "缺少 message")
        conv_id, ctype, target = self._resolve_target(params, message_type)

        mt, content, mentioned, extras = self.build_dy_payload(message)

        self._throttle()
        try:
            # Incoming IM frames already contain the conversation short id.
            # Reuse it for replies; asking create_conversation again is both
            # slower and rejected by newer sessions with "unexpected session
            # length" even while the incoming WS is still healthy.
            short_id = self.idmap.get_conversation_short_id(conv_id, ctype)
            ticket_info = self.im.get_ticket(
                conv_id, ctype, short_id=short_id or None)
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"取会话票失败: {err}") from err

        ref = None
        if extras.get("reply"):
            original = self.idmap.get_message(extras["reply"])
            if original:
                ref = {"id": original["server_message_id"],
                       "sender": str(original.get("sender") or ""),
                       "ts": int(time.time() * 1000)}

        text = cq.segments_to_plain(cq.normalize_segments(message))
        log("发送消息：type=%s target=%s text=%s" %
            (message_type or ctype, target or conv_id, text[:2000]))

        try:
            okflag, sid, detail = self.im.send(
                conv_id, ctype, mt, content,
                mentioned_users=mentioned or None,
                ref=ref,
                short_id=ticket_info.get("short_id"),
            )
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"发送时取会话票失败: {err}") from err
        if not okflag:
            raise ActionError(RET_BAD_REQUEST, f"发送失败: {detail}")

        self._remember(sid, ticket_info.get("conv_id") or conv_id, ctype,
                       sender=self.self_id, text=text)
        return {"message_id": int(sid) if sid else int(time.time() * 1000)}

    def send_private_msg(self, params):
        return self._do_send(params, "private")

    def send_group_msg(self, params):
        return self._do_send(params, "group")

    def send_msg(self, params):
        mt = params.get("message_type")
        if mt is None:
            mt = "group" if params.get("group_id") not in (None, "") else "private"
        return self._do_send(params, mt)

    # ------------------------------------------------------------ 消息操作
    def delete_msg(self, params):
        """撤回。抖音 cmd 702，判定靠 message == "OK"（实测）。"""
        (message_id,) = _need(params, "message_id")
        mid = _as_int(message_id, "message_id")
        rec = self.idmap.get_message(mid)
        if not rec:
            raise ActionError(RET_BAD_REQUEST,
                              f"未知 message_id={mid}（只记得住最近 2000 条）")
        if rec.get("recalled"):
            return {}
        try:
            okflag, detail = self.im.recall(
                rec["conversation_id"], rec["conversation_type"],
                rec["server_message_id"])
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"撤回失败: {err}") from err
        if not okflag:
            raise ActionError(RET_BAD_REQUEST, f"撤回失败: {detail}")
        self.idmap.mark_recalled(mid)
        return {}

    def get_msg(self, params):
        (message_id,) = _need(params, "message_id")
        mid = _as_int(message_id, "message_id")
        rec = self.idmap.get_message(mid)
        if not rec:
            raise ActionError(RET_BAD_REQUEST, f"未知 message_id={mid}")
        segments = [{"type": "text", "data": {"text": rec.get("text") or ""}}]
        return {
            "message_id": mid,
            "real_id": mid,
            "sender": {"user_id": int(rec.get("sender") or 0),
                       "nickname": self.nicknames.get(str(rec.get("sender")), "")},
            "time": int(time.time()),
            "message_type": "group" if int(rec["conversation_type"]) == CONV_GROUP
                            else "private",
            "message": segments,
            "raw_message": rec.get("text") or "",
        }

    # ------------------------------------------------------------ 群
    def get_group_msg_history(self, params):
        """群历史消息。

        抖音这个接口单次最多只给 50 条（实测 42 条就 has_more=false），
        所以这里就**如实返回**，不做多页拼接假装有更多：count 超过 50 按 50 截，
        实际几条就回几条。上层看到的数量就是抖音真实给的数量。
        """
        (group_id,) = _need(params, "group_id")
        gid = _as_int(group_id, "group_id")
        count = params.get("count")
        limit = 50
        if count not in (None, ""):
            try:
                limit = max(1, min(50, int(count)))
            except (TypeError, ValueError):
                limit = 50
        try:
            raw_msgs, _next, _more = self.im.history(str(gid), CONV_GROUP,
                                                     limit=limit)
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"取群历史失败: {err}") from err

        out = []
        for m in raw_msgs:
            d = im_message_to_dict(m)
            segs = cq.dy_content_to_segments(d.get("message_type"),
                                            d.get("content"),
                                            idmap=self.idmap)
            if not segs:
                continue
            sender = str(d.get("sender") or "")
            mid = int(d.get("server_message_id") or 0)
            ts = int(d.get("create_time") or 0)
            if ts > 10 ** 12:      # 抖音给的是毫秒
                ts //= 1000
            out.append({
                "message_id": mid,
                "real_id": mid,
                "sender": {"user_id": self.idmap.to_onebot(sender),
                           "nickname": self.nicknames.get(sender) or "",
                           "role": "member"},
                "time": ts,
                "message_type": "group",
                "message": segs,
                "raw_message": cq.segments_to_cq(segs),
            })
        # 按时间正序，和 OneBot 客户端的预期一致
        out.sort(key=lambda x: x["time"])
        return out

    def get_group_info(self, params):
        (group_id,) = _need(params, "group_id")
        gid = _as_int(group_id, "group_id")
        try:
            info = self.im.conversation_info(str(gid), CONV_GROUP)
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"取群信息失败: {err}") from err
        return {
            "group_id": gid,
            "group_name": info.get("name") or "",
            "member_count": int(info.get("participants_count") or 0),
            "max_member_count": int(info.get("participants_count") or 0),
            "owner_id": int(info.get("owner") or 0),
            "group_memo": "",
        }

    def get_group_list(self, params=None):
        rows, _more, _total = self.im.conversation_list(CONV_GROUP)
        return [{
            "group_id": int(r["conversation_id"]),
            "group_name": r.get("name") or "",
            "member_count": int(r.get("participant_count") or 0),
            "max_member_count": int(r.get("participant_count") or 0),
        } for r in rows if str(r["conversation_id"]).isdigit()]

    def _member_obj(self, group_id, p):
        # user_id 走 idmap：机器人自己的 OneBot id 与抖音 uid 不同区间，
        # 直接回抖音 uid 会和事件里的 self_id 对不上（@自己 就匹配不到）。
        uid = self.idmap.to_onebot(p["user_id"])
        nick = self.nicknames.get(str(p["user_id"])) or ""
        return {
            "group_id": group_id,
            "user_id": uid,
            "nickname": nick,
            "card": p.get("alias") or "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": 0,
            "last_sent_time": 0,
            "level": "0",
            "role": ROLE_ONEBOT.get(int(p.get("role") or 0), "member"),
            "unfriendly": bool(p.get("blocked")),
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
        }

    def get_group_member_list(self, params):
        (group_id,) = _need(params, "group_id")
        gid = _as_int(group_id, "group_id")
        try:
            rows = self.im.participants(str(gid), CONV_GROUP, limit=200)
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"取群成员失败: {err}") from err
        for p in rows:
            # 顺手把角色落缓存：抖音的推送里不带角色，事件里的 sender.role
            # 只能靠群成员接口这趟拿到。
            try:
                self.idmap.cache_profile(p["user_id"],
                                         sec_uid=p.get("sec_uid") or "",
                                         role=int(p.get("role") or 0),
                                         group_id=gid)
            except Exception:
                pass
        return [self._member_obj(gid, p) for p in rows]

    def get_group_member_info(self, params):
        group_id, user_id = _need(params, "group_id", "user_id")
        gid, uid = _as_int(group_id, "group_id"), _as_int(user_id, "user_id")
        # 上游给的是 OneBot id，participants 回的是抖音 uid，比对前先换回去。
        dy_uid = self.idmap.to_douyin(uid)
        try:
            rows = self.im.participants(str(gid), CONV_GROUP, limit=200)
        except DouyinIMError as err:
            raise ActionError(RET_BAD_REQUEST, f"取群成员失败: {err}") from err
        hit = next((p for p in rows if str(p["user_id"]) == str(dy_uid)), None)
        if not hit:
            raise ActionError(RET_BAD_REQUEST, f"群 {gid} 里没有成员 {uid}")
        obj = self._member_obj(gid, hit)
        if not obj["nickname"] and hit.get("sec_uid"):
            prof = self.im.profile(uid=int(dy_uid) if str(dy_uid).isdigit() else dy_uid,
                                   sec_uid=hit["sec_uid"])
            if prof.get("nickname"):
                obj["nickname"] = prof["nickname"]
                self.nicknames[str(dy_uid)] = prof["nickname"]
        return obj

    # ------------------------------------------------------------ 用户 / 好友
    def get_stranger_info(self, params):
        (user_id,) = _need(params, "user_id")
        uid = _as_int(user_id, "user_id")
        cached = self.idmap.get_profile(uid)
        prof = {}
        try:
            prof = self.im.profile(uid=uid, sec_uid=cached.get("sec_uid") or "",
                                   use_cache=not params.get("no_cache"))
        except Exception:
            prof = {}
        nick = prof.get("nickname") or cached.get("nickname") or ""
        if nick:
            self.nicknames[str(uid)] = nick
            self.idmap.cache_profile(uid, nick, prof.get("sec_uid") or "",
                                     prof.get("avatar") or "")
        return {"user_id": uid, "nickname": nick, "sex": "unknown",
                "age": 0, "level": "0"}

    def get_friend_list(self, params=None):
        """抖音好友 = 单聊会话列表（实测 con_type=1）。

        抖音会话列表里混着一条「和自己聊」的会话
        （conversation_id = "0:1:<self>:<self>"），那不是好友，要剔掉。
        """
        rows, _more, _total = self.im.conversation_list(CONV_ONE_TO_ONE)
        self_uid = str(self.im.self_uid)
        out, seen = [], set()
        for r in rows:
            peer = r.get("peer") or ""
            if not peer.isdigit() or peer == self_uid or peer in seen:
                continue
            seen.add(peer)
            nick = self.idmap.get_profile(peer).get("nickname") or ""
            out.append({"user_id": int(peer), "nickname": nick, "remark": ""})
        return out

    def get_login_info(self, params=None):
        uid = self.im.self_uid
        nick = self.idmap.get_profile(uid).get("nickname") or ""
        if not nick:
            try:
                prof = self.im.profile(uid=uid)
                nick = prof.get("nickname") or ""
                if nick:
                    self.idmap.cache_profile(uid, nick, prof.get("sec_uid") or "",
                                             prof.get("avatar") or "")
            except Exception:
                nick = ""
        return {"user_id": self.self_id, "nickname": nick or "抖音用户"}

    # ------------------------------------------------------------ 元信息
    def get_version_info(self, params=None):
        return {"app_name": "dyonebot", "app_version": "1.0.0",
                "protocol_version": "v11", "app_full_name": "dyonebot 抖音 OneBot 桥"}

    def get_status(self, params=None):
        return {
            "online": True, "good": True,
            "stat": {"packet_received": 0, "packet_sent": 0,
                     "message_received": 0, "message_sent": 0,
                     "packet_lost": 0, "message_lost": 0},
        }

    def can_send_image(self, params=None):
        return {"yes": True}

    def can_send_record(self, params=None):
        return {"yes": False}

    def get_online_clients(self, params=None):
        return {"clients": []}

    # ------------------------------------------------------------ 媒体取回
    def get_image(self, params):
        """OneBot 的 get_image：把 file 换成可下载的 url。

        事件里给的已经是本桥解密服务的 http URL，直接回。若对方传的是一个
        裸 token（老版本客户端会把 file 里的 token 单独拿出来），也认。
        """
        (file,) = _need(params, "file")
        ref = str(file)
        if _HTTP_RE.match(ref):
            return {"file": ref, "url": ref}
        media = getattr(self, "media", None)
        if media is not None and media.url_for(ref):
            url = media.url_for(ref)
            return {"file": url, "url": url}
        raise ActionError(RET_BAD_REQUEST,
                          f"无法解析的图片引用: {ref!r}（需要 http URL 或媒体 token）")

    def get_record(self, params):
        return self.get_image(params)

    def get_file(self, params):
        return self.get_image(params)

    # ------------------------------------------------------------ 抖音没有的能力
    def _unsupported(self, name):
        def handler(params):
            raise ActionError(RET_BAD_REQUEST,
                              f"抖音 IM 没有 {name} 能力，本桥不伪造成功")
        return handler

    def get_group_honor_info(self, params):
        return self._unsupported("群荣誉")(params)

    def get_group_system_msg(self, params=None):
        return {"invited_requests": [], "join_requests": []}

    def set_group_ban(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有禁言接口")

    def set_group_whole_ban(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有全员禁言接口")

    def set_group_kick(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有踢人接口")

    def set_group_leave(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有退群接口")

    def set_group_card(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有群名片接口")

    def set_group_name(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有改群名接口")

    def set_friend_add_request(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有好友申请接口")

    def send_like(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有点赞接口")

    def send_group_forward_msg(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有合并转发接口")

    def send_private_forward_msg(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有合并转发接口")

    def get_forward_msg(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有合并转发接口")

    def get_group_file_system_info(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有群文件系统")

    def get_group_root_files(self, params):
        raise ActionError(RET_BAD_REQUEST, "抖音 IM 没有群文件系统")

    def get_private_file_url(self, params):
        return self.get_file(params)

    def get_group_file_url(self, params):
        return self.get_file(params)

    def set_restart(self, params=None):
        raise ActionError(RET_BAD_REQUEST, "本桥不支持远程重启")

    def _get_cookies(self, params=None):
        raise ActionError(RET_BAD_REQUEST, "抖音登录态不外泄")

    def get_csrf_token(self, params=None):
        return {"token": 0}

    # ------------------------------------------------------------ 分发
    def handle(self, action, params):
        handler = getattr(self, action, None)
        if handler is None or not callable(handler):
            raise ActionError(RET_BAD_HANDLER, f"未实现的 action: {action}")
        return handler(params or {})


# 抖音角色常量在这里再导出一份，方便外部测试引用
__all__ = ["Actions", "ActionError", "RET_OK", "RET_BAD_HANDLER",
           "RET_BAD_PARAMS", "RET_BAD_REQUEST", "ROLE_MEMBER"]
