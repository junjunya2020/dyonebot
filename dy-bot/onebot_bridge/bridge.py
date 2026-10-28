# -*- coding: utf-8 -*-
"""OneBot v11 适配层：正向 WS + 反向 WS + HTTP API + HTTP POST 事件上报。

一个进程同时提供四种通信方式（都能关），都喂同一份事件流、共用同一张 action 表：

    正向 WS   本桥 listen(PORT)，OneBot 客户端连进来
              —— 也就是 AstrBot 的「OneBot v11 反向 WS」/ NapCat 的 ws 反连端
    反向 WS   本桥主动连对方（AstrBot 默认的 aiocqhttp ws_reverse 就在这里）
    HTTP API  本桥 listen(PORT)，POST /<action> JSON，同步回结果
    HTTP POST 把事件 POST 到配置的 URL（OneBot v11 的 http 上报）

协议要点（实测，见 ONEBOT-对接说明.md）：
  * `message` 必须是【数组】，字符串会被 AstrBot 判非法；
  * id 必须是 JSON number 且在 int64 内；
  * `echo` 必须原样回传，不回会让对方挂到 api_timeout；
  * 未实现的 action 要显式回 failed/1404，绝不能沉默。
"""
import asyncio
import json
import time
import traceback

import websockets

from . import cq
from .actions import ActionError, Actions, RET_BAD_HANDLER
from .dyim import (CONV_GROUP, CONV_ONE_TO_ONE, DouyinIMError, ROLE_ONEBOT,
                   im_message_to_dict, is_recalled, parse_push)
from .logutil import log


# ---------------------------------------------------------------- 事件构造

def _sender_obj(onebot_uid, nickname="", role="member", *, douyin_id="",
                sec_uid="", unique_id=""):
    out = {"user_id": int(onebot_uid), "nickname": nickname or "",
           "sex": "unknown", "age": 0, "role": role}
    # 扩展字段不改变 OneBot v11 原字段，供绑定/签到桥使用。
    if douyin_id:
        out["douyin_id"] = str(douyin_id)
    if sec_uid:
        out["douyin_sec_uid"] = str(sec_uid)
    if unique_id:
        out["douyin_unique_id"] = str(unique_id)
    return out


class EventBuilder:
    """抖音推送 -> OneBot v11 事件 dict。

    注意：这里【绝不】做网络请求。昵称一律从 idmap 的本地缓存读，
    缓存由后台预取线程填。事件循环里出现一次 profile HTTP 调用就会把
    整个桥卡住几百毫秒。
    """

    def __init__(self, idmap, self_id, nicknames, config=None, self_uid="",
                 media=None):
        self.idmap = idmap
        self.self_id = int(self_id)
        self.nicknames = nicknames
        self.config = config or {}
        self.self_uid = str(self_uid or "")
        self.media = media       # MediaCache：把加密图片换成可显示的代理 URL

    def nickname(self, uid, sec_uid=""):
        uid = str(uid or "")
        hit = self.idmap.get_profile(uid).get("nickname")
        if hit:
            return hit
        return self.nicknames.get(uid, "")

    def _mention_names(self, content):
        """给 @ 还原用的昵称表。

        只读本地缓存（idmap 持久化的 profiles + 进程内热表），
        绝不在这里发 HTTP —— 事件循环里一次网络往返就够把桥卡住。
        """
        names = dict(self.nicknames)
        if not isinstance(content, dict):
            return names
        for key in ("mention_users", "mentioned_users"):
            for item in content.get(key) or []:
                uid = item.get("uid") or item.get("user_id") \
                    if isinstance(item, dict) else item
                if not uid:
                    continue
                uid = str(uid)
                if names.get(uid):
                    continue
                nick = self.idmap.get_profile(uid).get("nickname")
                if nick:
                    names[uid] = nick
        return names

    def _message_id(self, msg):
        """OneBot message_id 直接用抖音的 server_message_id。

        抖音的 server_message_id 本身就是 int64（实测 7.6e18，未超上限），
        直接当 message_id 可以让撤回/引用天然对齐，不需要再发号。
        """
        mid = int(msg.get("server_message_id") or 0)
        return mid or int(time.time() * 1000)

    def message_event(self, msg):
        """构造 message 事件。返回 None 表示这条不该推给 OneBot。"""
        mtype = int(msg.get("conversation_type") or 0)
        dy_mt = int(msg.get("message_type") or 0)
        sender = str(msg.get("sender") or "")
        conv_id = str(msg.get("conversation_id") or "")

        if self.config.get("skip_self", True) and sender == str(self.self_id_uid):
            return None

        nickname = self.nickname(sender, msg.get("sender_sec_uid"))
        cached_profile = self.idmap.get_profile(sender)
        sender_sec_uid = str(msg.get("sender_sec_uid")
                             or cached_profile.get("sec_uid") or "")
        douyin_unique_id = str(cached_profile.get("unique_id") or "")
        segments = cq.dy_content_to_segments(dy_mt, msg.get("content"),
                                             self._mention_names(msg.get("content")),
                                             media=self.media,
                                             idmap=self.idmap)
        if not segments:
            return None
        raw = cq.segments_to_cq(segments)
        mid = self._message_id(msg)
        user_id = self.idmap.to_onebot(sender)
        ts = int(msg.get("create_time") or 0)
        if ts > 10 ** 12:
            ts //= 1000
        if ts <= 0:
            ts = int(time.time())

        self.idmap.remember_message(mid, conv_id, mtype, mid,
                                    sender=user_id, text=raw,
                                    conversation_short_id=int(
                                        msg.get("conversation_short_id") or 0))

        if mtype == CONV_GROUP:
            group_id = int(conv_id) if conv_id.isdigit() \
                else self.idmap.to_onebot(conv_id)
            # 推送里【不带】发送者角色，用群成员预取时缓存下来的角色；
            # 查不到就按 member，宁可保守也不编造管理员。
            dy_role = self.idmap.get_role(sender, group_id)
            if dy_role is None:
                dy_role = int(msg.get("sender_role") or 0)
            role = ROLE_ONEBOT.get(int(dy_role), "member")
            return {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "time": ts,
                "self_id": self.self_id,
                "group_id": group_id,
                "user_id": user_id,
                "message_id": mid,
                "raw_message": raw,
                "font": 0,
                "sender": _sender_obj(
                    user_id, nickname, role,
                    douyin_id=sender, sec_uid=sender_sec_uid,
                    unique_id=douyin_unique_id),
                "message": segments,
            }

        return {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "time": ts,
            "self_id": self.self_id,
            "user_id": user_id,
            "message_id": mid,
            "raw_message": raw,
            "font": 0,
            "sender": _sender_obj(
                user_id, nickname, "member",
                douyin_id=sender, sec_uid=sender_sec_uid,
                unique_id=douyin_unique_id),
            "message": segments,
        }

    @property
    def self_id_uid(self):
        return self.self_uid

    def recall_event(self, msg):
        """抖音撤回（message_type 40001，或历史里被标记的 s:is_recalled）。"""
        ext = msg.get("ext") or {}
        target = ext.get("s:target_server_message_id") or ""
        if not str(target).isdigit():
            return None
        operator = str(ext.get("s:recall_uid")
                       or ext.get("s:target_sender") or msg.get("sender") or "")
        role = ext.get("s:recall_role") or "0"
        conv_id = str(msg.get("conversation_id") or "")
        mtype = int(msg.get("conversation_type") or 0)
        op_id = self.idmap.to_onebot(operator)
        base = {
            "post_type": "notice",
            "time": int(msg.get("create_time") or time.time()),
            "self_id": self.self_id,
            "user_id": op_id,
            "operator_id": op_id,
        }
        if base["time"] > 10 ** 12:
            base["time"] //= 1000
        if mtype == CONV_GROUP:
            base.update({
                "notice_type": "group_recall",
                "sub_type": "recall",
                "group_id": int(conv_id) if conv_id.isdigit()
                            else self.idmap.to_onebot(conv_id),
                "message_id": int(target),
            })
        else:
            base.update({
                "notice_type": "friend_recall",
                "message_id": int(target),
            })
        return base

    def meta_event(self, sub_type="connect", interval=0):
        return {
            "post_type": "meta_event",
            "meta_event_type": "lifecycle" if sub_type in ("connect", "enable")
                               else "heartbeat",
            "sub_type": sub_type,
            "time": int(time.time()),
            "self_id": self.self_id,
            "interval": int(interval),
        }


# ---------------------------------------------------------------- 连接管理

class WsSink:
    """一条 OneBot 连接。

    正向 WS 每来一个客户端建一个；反向 WS 每次重连建一个。
    发送全部串到一个 asyncio 队列，避免多个事件泵并发写在同一个 socket 上
    交错（OneBot 的 JSON 帧不接受交错写）。
    """

    def __init__(self, ws, role="universal", name="ws"):
        self.ws = ws
        self.role = role
        self.name = name
        self.alive = True
        self.queue = asyncio.Queue(maxsize=2000)
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while self.alive:
                payload = await self.queue.get()
                if payload is None:
                    break
                await self.ws.send(payload)
        except Exception as err:
            log(f"{self.name} 发送循环结束: {type(err).__name__}: {err}", "WARN")
        finally:
            self.alive = False

    def send_event(self, event):
        if not self.alive:
            return
        try:
            self.queue.put_nowait(json.dumps(event, ensure_ascii=False))
        except asyncio.QueueFull:
            log(f"{self.name} 事件队列满，丢弃一条", "WARN")

    def send_json(self, obj):
        """API 回执走专用路径：语义上必须尽快回，不排事件后面。"""
        if not self.alive:
            return None
        return self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def close(self):
        self.alive = False
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass
        if self._task:
            self._task.cancel()


class Bridge:
    def __init__(self, config, im, idmap, self_id, media=None):
        self.config = config
        self.im = im
        self.idmap = idmap
        self.self_id = int(self_id)
        self.media = media             # MediaCache（见 media.py）
        self.nicknames = {}            # uid -> nickname（进程内热缓存）
        self.builder = EventBuilder(idmap, self_id, self.nicknames, config,
                                    self_uid=im.self_uid, media=media)
        self.actions = Actions(im, idmap, config, self_id, self.nicknames,
                               media=media)
        self.sinks = []                # [WsSink] 正向 + 反向
        self.post_urls = []
        self._seen_messages = set()
        self._share_history_pending = set()
        self._seen_recall = set()
        self._recall_seen_targets = set()
        self._stop = asyncio.Event()
        self._loop = None
        self.supervisor = None         # UpstreamSupervisor（_douyin_pump 里建）
        self.login = None              # LoginManager（run 里建）
        self.webui = None              # WebUI（run 里建）
        # 面板/状态用的计数（只增不减，进程内有效）
        self.stat_messages_in = 0      # 上游推来的消息条数
        self.stat_events_out = 0       # 广播给 OneBot 的事件条数
        self.stat_actions = 0          # 处理过的 action 次数
        self.stat_errors = 0

    # ------------------------------------------------------------ 启动
    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._build_supervisor()
        tasks = []
        fw = self.config.get("forward_ws") or {}
        if fw.get("enabled"):
            tasks.append(asyncio.create_task(self._forward_ws_server(fw)))
        rv = self.config.get("reverse_ws") or {}
        if rv.get("enabled") and rv.get("urls"):
            for url in rv["urls"]:
                tasks.append(asyncio.create_task(self._reverse_ws_client(url, rv)))
        ha = self.config.get("http_api") or {}
        if ha.get("enabled"):
            tasks.append(asyncio.create_task(self._http_api_server(ha)))
        hp = self.config.get("http_post") or {}
        if hp.get("enabled") and hp.get("urls"):
            self.post_urls = list(hp["urls"])
        if self.config.get("profile_prefetch", {}).get("enabled"):
            tasks.append(asyncio.create_task(self._prefetch_profiles()))
        tasks.append(asyncio.create_task(self._recall_poller()))
        tasks.append(asyncio.create_task(self._heartbeat()))
        tasks.append(asyncio.create_task(self._douyin_pump()))
        tasks.append(asyncio.create_task(self._webui_server()))
        tasks.append(asyncio.create_task(self._media_server()))
        await self._stop.wait()
        self._shutdown()
        for t in tasks:
            t.cancel()

    def _shutdown(self):
        """停掉非 asyncio 的常驻件（看护线程 / 登录线程）。"""
        if self.supervisor is not None:
            try:
                self.supervisor.stop()
            except Exception as err:
                log(f"停止上游看护失败: {type(err).__name__}: {err}", "WARN")

    # ------------------------------------------------------------ 媒体服务
    async def _media_server(self):
        """把解密后的图片用 HTTP 回给 OneBot 客户端。

        为什么必须单独起一个端口：OneBot 客户端（AstrBot）取图片时不带任何
        凭据，而面板 6800 是有 token 鉴权的；混在一起要么把面板打开成无鉴权，
        要么客户端全 401。所以这里单开一个只读、只认 token 的图片服务，
        路径形如 /media/<token>，token 就是抖音给的图片 md5（不是秘密，
        拿到也只能换回那一张图）。

        真正的下载+解密发生在有人来取的那一刻，事件构造路径上零阻塞。
        """
        if self.media is None or not self.media.enabled:
            log("图片解密服务已禁用（media.enabled=false）")
            return
        from aiohttp import web
        app = web.Application()
        self.media.routes(app, web)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.media.port)
        try:
            await site.start()
        except OSError as err:
            log(f"图片服务端口 {self.media.port} 绑定失败（{err}），"
                f"图片将退回加密直链", "WARN")
            return
        log(f"图片解密服务监听 {self.media.public_base}"
            f"{self.media.path}/<token>（0.0.0.0:{self.media.port}）")
        await asyncio.Future()

    # ------------------------------------------------------------ 控制面板
    async def _webui_server(self):
        """起 WebUI（面板 token 与 OneBot 的 WS token 是两套独立凭据）。"""
        conf = self.config.get("webui") or {}
        if not conf.get("enabled", True):
            log("控制面板已禁用（webui.enabled=false）")
            return
        from .login import LoginManager
        from .logutil import get_hub
        from .webui import WebUI

        hub = get_hub()
        if hub is not None:
            # 日志可能来自别的线程（上游看护线程 / 登录线程）。绑定事件循环后
            # 它们会用 call_soon_threadsafe 投递，SSE 才能真正实时推。
            hub.bind_loop(asyncio.get_running_loop())

        self.login = LoginManager(
            timeout=int(conf.get("login_timeout_sec", 600) or 600),
            initial_uid=self.im.self_uid,
            on_success=self._on_login_success)
        self.webui = WebUI(self.config, self, supervisor=self.supervisor,
                           login_manager=self.login, hub=hub)
        await self.webui.serve()
        await asyncio.Future()

    def _on_login_success(self, auth):
        """面板扫码成功后：把新凭据交给看护线程，让它换 token 重连。

        在登录线程里被调用，所以只做「通知 + 唤醒」，不碰 asyncio 对象。
        """
        try:
            self.im.auth = auth
            self.im._uid = str(auth.get_uid())
        except Exception as err:
            log(f"登录后更新 IM 门面失败: {type(err).__name__}: {err}", "WARN")
        if self.supervisor is not None:
            self.supervisor.request_token_reload("面板扫码登录成功")

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

    # ------------------------------------------------------------ 正向 WS
    async def _forward_ws_server(self, conf):
        host = conf.get("host", "127.0.0.1")
        port = int(conf.get("port", 6700))
        path = conf.get("path") or "/"
        token = conf.get("access_token") or ""

        async def handler(ws):
            conn = getattr(ws, "request", None)
            req_path = "/"
            headers = {}
            if conn is not None:
                req_path = getattr(conn, "path", "/") or "/"
                headers = {k.lower(): v for k, v in conn.headers.items()}
            if path not in ("/", "") and not req_path.startswith(path.rstrip("/")):
                await ws.close(code=1008, reason="bad path")
                return
            if token:
                got = headers.get("authorization", "")
                if got != "Bearer " + token and got != token:
                    await ws.close(code=1008, reason="bad token")
                    return
            role = headers.get("x-client-role", "universal")
            name = f"正向WS {headers.get('x-self-id','?')}"
            sink = WsSink(ws, role=role, name=name)
            sink.start()
            self.sinks.append(sink)
            log(f"{name} 已连接 role={role}")
            await self._send_hello(sink)
            try:
                await self._serve_api_loop(ws, sink)
            finally:
                sink.alive = False
                if sink in self.sinks:
                    self.sinks.remove(sink)
                log(f"{name} 断开")

        while True:
            try:
                async with websockets.serve(handler, host, port,
                                            max_size=64 * 1024 * 1024):
                    log(f"正向 WS 监听 ws://{host}:{port}{path}")
                    await asyncio.Future()
            except asyncio.CancelledError:
                raise
            except OSError as err:
                log(f"正向 WS 端口 {port} 绑定失败（{err}），5 秒后重试", "WARN")
                await asyncio.sleep(5)

    # ------------------------------------------------------------ 反向 WS
    async def _reverse_ws_client(self, url, conf):
        token = conf.get("access_token") or ""
        backoff = float(conf.get("reconnect_min_sec", 1.0))
        max_backoff = float(conf.get("reconnect_max_sec", 30.0))
        while not self._stop.is_set():
            headers = {"X-Self-ID": str(self.self_id),
                       "X-Client-Role": "universal",
                       "User-Agent": "OneBot/11 dyonebot"}
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                async with websockets.connect(
                        url, additional_headers=headers, max_size=64 * 1024 * 1024,
                        ping_interval=20, ping_timeout=20) as ws:
                    sink = WsSink(ws, name=f"反向WS {url}")
                    sink.start()
                    self.sinks.append(sink)
                    log(f"反向 WS 已连上 {url}")
                    backoff = float(conf.get("reconnect_min_sec", 1.0))
                    await self._send_hello(sink)
                    await self._serve_api_loop(ws, sink)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log(f"反向 WS {url} 断开: {type(err).__name__}: {err}", "WARN")
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _send_hello(self, sink):
        sink.send_event(self.builder.meta_event("connect"))

    async def _serve_api_loop(self, ws, sink):
        """收对方的 action 调用，分发到线程池，回 echo。"""
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or "action" not in payload:
                continue
            asyncio.create_task(self._dispatch(sink, payload))

    async def _dispatch(self, sink, payload):
        action = payload.get("action")
        params = payload.get("params") or {}
        echo = payload.get("echo")
        self._bump("stat_actions")
        try:
            data = await asyncio.to_thread(self.actions.handle, action, params)
            # 空列表必须原样回 []，不能塌成 {}：get_group_list / get_friend_list
            # 的 data 类型是数组，塌了会让上层解析报错。
            reply = {"status": "ok", "retcode": 0,
                     "data": {} if data is None else data, "echo": echo}
        except ActionError as err:
            reply = {"status": "failed", "retcode": err.retcode, "msg": err.msg,
                     "echo": echo}
        except NotImplementedError as err:
            reply = {"status": "failed", "retcode": RET_BAD_HANDLER,
                     "msg": str(err), "echo": echo}
        except Exception as err:
            log(f"action {action} 异常: {type(err).__name__}: {err}\n"
                + traceback.format_exc(), "ERROR")
            reply = {"status": "failed", "retcode": 1500,
                     "msg": f"{type(err).__name__}: {err}", "echo": echo}
        try:
            await sink.send_json(reply)
        except Exception as err:
            log(f"回执发送失败 action={action}: {err}", "WARN")

    # ------------------------------------------------------------ HTTP API
    async def _http_api_server(self, conf):
        """极简 OneBot v11 HTTP API：POST /<action>，body 是 params。

        不用 aiohttp 依赖，直接用 asyncio 起一个够用的 HTTP/1.1 服务，
        因为这里只需要「读一个 POST body + 回一个 JSON」。
        """
        host = conf.get("host", "127.0.0.1")
        port = int(conf.get("port", 6701))
        prefix = (conf.get("path") or "/").rstrip("/")
        token = conf.get("access_token") or ""

        async def handle(reader, writer):
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except Exception:
                writer.close()
                return
            lines = head.decode("latin-1").split("\r\n")
            request_line = lines[0].split(" ")
            method = request_line[0].upper() if request_line else "GET"
            target = request_line[1] if len(request_line) > 1 else "/"
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length") or 0)
            body = b""
            if length:
                body = await reader.readexactly(length)

            def respond(obj, code=200):
                raw = json.dumps(obj, ensure_ascii=False).encode()
                writer.write(
                    f"HTTP/1.1 {code} OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
                    .encode() + raw)

            try:
                if token:
                    got = headers.get("authorization", "")
                    if got not in ("Bearer " + token, token):
                        respond({"status": "failed", "retcode": 1403,
                                 "msg": "token 校验失败"}, 403)
                        return
                path = target.split("?", 1)[0]
                if prefix and path.startswith(prefix):
                    path = path[len(prefix):]
                action = path.strip("/").split("/")[-1] if path.strip("/") else ""
                if not action:
                    respond({"status": "failed", "retcode": 1404,
                             "msg": "缺少 action"}, 404)
                    return
                try:
                    params = json.loads(body) if body else {}
                except ValueError:
                    params = {}
                if method == "GET":
                    import urllib.parse
                    q = urllib.parse.parse_qs(target.split("?", 1)[1]) \
                        if "?" in target else {}
                    params = {k: v[0] for k, v in q.items()}
                data = await asyncio.to_thread(self.actions.handle, action,
                                               params)
                respond({"status": "ok", "retcode": 0,
                         "data": {} if data is None else data})
            except ActionError as err:
                respond({"status": "failed", "retcode": err.retcode,
                         "msg": err.msg})
            except Exception as err:
                log(f"HTTP action 异常: {type(err).__name__}: {err}", "ERROR")
                respond({"status": "failed", "retcode": 1500,
                         "msg": f"{type(err).__name__}: {err}"}, 500)
            finally:
                try:
                    await writer.drain()
                    writer.close()
                except Exception:
                    pass

        server = await asyncio.start_server(handle, host, port)
        log(f"HTTP API 监听 http://{host}:{port}{prefix or '/'}<action>")
        async with server:
            await server.serve_forever()

    # ------------------------------------------------------------ 事件分发
    def broadcast(self, event):
        if not event:
            return
        self._bump("stat_events_out")
        dead = []
        for sink in list(self.sinks):
            if sink.alive:
                sink.send_event(event)
            else:
                dead.append(sink)
        for sink in dead:
            if sink in self.sinks:
                self.sinks.remove(sink)
        if self.post_urls:
            asyncio.create_task(self._http_post(event))

    async def _http_post(self, event):
        import urllib.request
        conf = self.config.get("http_post") or {}
        secret = conf.get("secret") or ""
        timeout = float(conf.get("timeout_sec") or 8)
        data = json.dumps(event, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if secret:
            import hashlib
            import hmac
            headers["X-Signature"] = "sha1=" + hmac.new(
                secret.encode(), data, hashlib.sha1).hexdigest()
        for url in self.post_urls:
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                await asyncio.to_thread(
                    lambda r=req: urllib.request.urlopen(r, timeout=timeout).read())
            except Exception as err:
                log(f"HTTP POST 上报 {url} 失败: {type(err).__name__}: {err}",
                    "WARN")

    # ------------------------------------------------------------ 心跳
    async def _heartbeat(self):
        interval = int((self.config.get("forward_ws") or {}).get("heartbeat_sec")
                       or 0)
        while True:
            await asyncio.sleep(interval if interval > 0 else 3600)
            if interval > 0:
                self.broadcast(self.builder.meta_event("heartbeat", interval))

    # ------------------------------------------------------------ 抖音事件泵
    def _build_supervisor(self):
        """先建好看护对象（不启动），这样 WebUI 一定能拿到它。

        WebUI 任务和事件泵任务是并发起的，谁先跑不确定。若在看护任务里才
        创建 supervisor，面板可能先起、拿到 None 而看不到上游状态。
        """
        from .upstream import UpstreamSupervisor, reload_auth_from_env
        loop = asyncio.get_running_loop()

        def on_push(raw):
            loop.call_soon_threadsafe(self._emit_push, raw)

        def load_auth_and_update_im():
            """Reload the durable Auth and keep every bridge consumer in sync."""
            fresh = reload_auth_from_env()
            if fresh is not None:
                # The supervisor owns the WS Auth, while Actions/DouyinIM use
                # their own reference.  Updating only the former makes a
                # recovered WS look healthy but leaves sends/tickets on stale
                # credentials.
                try:
                    self.im.auth = fresh
                    self.im._uid = str(fresh.get_uid())
                except Exception as err:
                    log(f"重载登录态后更新 IM 门面失败: {type(err).__name__}: {err}",
                        "WARN")
            return fresh

        self.supervisor = UpstreamSupervisor(
            self.im.auth, self.config, on_push,
            auth_loader=load_auth_and_update_im)
        return self.supervisor

    async def _douyin_pump(self):
        """启动上游看护线程，把推送帧切回事件循环。

        上游是 websocket-client（阻塞）且自带的重连循环在「心跳超时」路径上
        失效（见 upstream.py 顶部的说明），所以重连策略由 UpstreamSupervisor
        拥有：上游只负责「一次尝试」，掉线由看护线程按三级策略自愈。

        on_push 做成「收帧 -> call_soon_threadsafe 回主循环」，
        看护线程里绝不做任何 asyncio 操作。
        """
        supervisor = self.supervisor or self._build_supervisor()
        supervisor.start()
        # 起完就常驻：看护线程自己活着，事件循环这边只是挂住等停止
        await asyncio.Future()

    def _emit_push(self, raw):
        """在事件循环里把一帧推送转成 OneBot 事件。"""
        try:
            kind, payload = parse_push(raw)
        except Exception as err:
            self._bump("stat_errors")
            log(f"推送解析失败: {type(err).__name__}: {err}", "WARN")
            return
        # 先记录所有成功解析的抖音推送，再按业务规则过滤；这样入群通知、
        # 撤回、已读、未知消息类型等也不会在日志里悄悄消失。
        if isinstance(payload, dict):
            try:
                detail = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                detail = repr(payload)
            log(
                "收到消息：kind=%s message_type=%s conversation_type=%s "
                "conversation_id=%s sender=%s payload=%s" % (
                    kind,
                    payload.get("message_type"),
                    payload.get("conversation_type"),
                    payload.get("conversation_id"),
                    payload.get("sender"),
                    detail[:2000],
                )
            )
        else:
            log(f"收到消息：kind={kind} payload={str(payload)[:2000]}")
        if kind != "message":
            return
        self._bump("stat_messages_in")
        self._on_douyin_message(payload)

    def _bump(self, name, delta=1):
        try:
            setattr(self, name, getattr(self, name, 0) + delta)
        except Exception:
            pass

    def _on_douyin_message(self, msg):
        """在事件循环里处理一条抖音推送（只做内存操作）。"""
        dy_mt = int(msg.get("message_type") or 0)

        # Type 77 push frames intentionally carry only a thin card.  The
        # complete item id/first-frame data is present in conversation history,
        # so enrich it before building the OneBot event.
        if dy_mt == 77 and not msg.get("_history_enriched"):
            key = (str(msg.get("conversation_id") or ""),
                   int(msg.get("server_message_id") or 0))
            if key not in self._share_history_pending:
                self._share_history_pending.add(key)
                asyncio.create_task(self._enrich_share_photo(msg, key))
                return

        if dy_mt == 40001:
            ev = self.builder.recall_event(msg)
            if ev:
                key = (ev.get("group_id"), ev.get("message_id"))
                if key not in self._seen_recall:
                    self._seen_recall.add(key)
                    self.broadcast(ev)
            return

        # 1001 是群系统通知（入群/退群/公告），不是用户发言
        if dy_mt == 1001 and not self.config.get("emit_notices"):
            return
        if dy_mt in (1, 1001) and not self.config.get("emit_notices"):
            content = msg.get("content") or {}
            if isinstance(content, dict) and (content.get("tips")
                                              or content.get("aweType") == 903):
                return

        # 忽略「对方已读」类事件
        if dy_mt in (50001, 50002):
            return

        ev = self.builder.message_event(msg)
        if not ev:
            return
        key = ev.get("message_id")
        if key in self._seen_messages:
            return
        self._seen_messages.add(key)
        if len(self._seen_messages) > 5000:
            self._seen_messages = set(list(self._seen_messages)[-2000:])
        self.broadcast(ev)

    async def _enrich_share_photo(self, msg, key):
        """Replace the truncated type-77 push card with its history payload."""
        try:
            conv_id = str(msg.get("conversation_id") or "")
            ctype = int(msg.get("conversation_type") or 0)
            short_id = int(msg.get("short_id") or
                           msg.get("conversation_short_id") or 0)
            rows, _cursor, _more = await asyncio.to_thread(
                self.im.history, conv_id, ctype, short_id=short_id, limit=30)
            target = int(msg.get("server_message_id") or 0)
            for row in rows:
                item = im_message_to_dict(row)
                if int(item.get("server_message_id") or 0) != target:
                    continue
                enriched = dict(msg)
                enriched["content"] = item.get("content") or msg.get("content")
                enriched["short_id"] = item.get("short_id") or short_id
                enriched["_history_enriched"] = True
                self._on_douyin_message(enriched)
                return
            log(f"图文消息历史中未找到 message_id={target}，使用推送卡片回退", "WARN")
        except Exception as err:
            log(f"图文消息历史补全失败: {type(err).__name__}: {err}", "WARN")
        finally:
            self._share_history_pending.discard(key)

        fallback = dict(msg)
        fallback["_history_enriched"] = True
        self._on_douyin_message(fallback)

    # ------------------------------------------------------------ 撤回对账
    async def _recall_poller(self):
        """抖音的撤回推送不稳定，用 301 历史接口定期对账补齐。

        实测：撤回既会作为 message_type 40001 推过来，历史里也会留下
        s:is_recalled=true。两条路都走，靠 (group_id, message_id) 去重。
        """
        conf = self.config.get("recall_poll") or {}
        if not conf.get("enabled"):
            return
        scope = conf.get("scope") or "groups"
        if scope == "none":
            return
        interval = max(5, int(conf.get("interval_sec") or 20))
        limit = int(conf.get("history_limit") or 30)
        await asyncio.sleep(8)
        while True:
            try:
                await asyncio.to_thread(self._poll_recalls_once, scope, limit)
            except Exception as err:
                log(f"撤回对账异常: {type(err).__name__}: {err}", "WARN")
            await asyncio.sleep(interval)

    def _poll_recalls_once(self, scope, limit):
        groups = []
        if scope in ("groups", "all"):
            try:
                rows, _m, _t = self.im.conversation_list(CONV_GROUP, limit=50)
                groups = [(r["conversation_id"], CONV_GROUP) for r in rows
                          if str(r["conversation_id"]).isdigit()]
            except Exception as err:
                log(f"撤回对账取群列表失败: {err}", "WARN")
        if scope == "all":
            try:
                rows, _m, _t = self.im.conversation_list(CONV_ONE_TO_ONE, limit=50)
                groups += [(r["peer"], CONV_ONE_TO_ONE) for r in rows
                           if r.get("peer")]
            except Exception:
                pass

        for conv_id, ctype in groups:
            try:
                msgs, _cur, _more = self.im.history(conv_id, ctype, limit=limit)
            except Exception:
                continue
            for m in msgs:
                d = im_message_to_dict(m)
                if int(d.get("message_type") or 0) == 40001:
                    self._loop.call_soon_threadsafe(self._on_douyin_message, d)
                    continue
                if not is_recalled(d):
                    continue
                ext = d.get("ext") or {}
                target = ext.get("s:target_server_message_id")
                if not str(target or "").isdigit():
                    continue
                d["message_type"] = 40001
                d["ext"] = dict(ext)
                d["ext"].setdefault("s:recall_uid", d.get("sender"))
                self._loop.call_soon_threadsafe(self._on_douyin_message, d)

    # ------------------------------------------------------------ 昵称预取
    async def _prefetch_profiles(self):
        conf = self.config.get("profile_prefetch") or {}
        await asyncio.sleep(3)
        if not conf.get("on_start", True):
            return
        try:
            await asyncio.to_thread(self._prefetch_once,
                                    int(conf.get("group_limit") or 20))
        except Exception as err:
            log(f"昵称预取失败: {type(err).__name__}: {err}", "WARN")

    def _prefetch_once(self, group_limit):
        """拉群成员列表 + 逐个补昵称，全部落 idmap 缓存。"""
        want = []
        self_uid = str(self.im.self_uid or "")
        # 自己的昵称先看缓存。profile(uid=) 不带 sec_uid 时主站接口必然回空
        # （实测：只给 uid -> {}；给 sec_uid -> nickname/avatar 都有），
        # 所以没缓存就等下面从群成员里捞到自己的 sec_uid 再补。
        try:
            if not self.idmap.get_profile(self_uid).get("nickname"):
                want.append((self_uid, ""))
        except Exception:
            pass

        try:
            rows, _m, _t = self.im.conversation_list(CONV_GROUP,
                                                     limit=group_limit)
        except Exception as err:
            log(f"昵称预取取群列表失败: {err}", "WARN")
            return

        for r in rows:
            gid = str(r.get("conversation_id") or "")
            if not gid.isdigit():
                continue
            try:
                members = self.im.participants(gid, CONV_GROUP, limit=200)
            except Exception:
                continue
            for p in members:
                uid = str(p.get("user_id") or "")
                if not uid:
                    continue
                role = int(p.get("role") or 0)
                cached = self.idmap.get_profile(uid)
                # 角色每次都刷新（会变），昵称只在还没有的时候才去补
                self.idmap.cache_profile(uid, sec_uid=p.get("sec_uid") or "",
                                         role=role, group_id=gid)
                if cached.get("nickname"):
                    self.nicknames[uid] = cached["nickname"]
                    continue
                want.append((uid, p.get("sec_uid") or ""))

        # 去重：同一个 uid 只查一次，已经有 sec_uid 的优先。
        # 自己的条目最先是 (self_uid, "")，群成员那轮会带上真实 sec_uid 补进来。
        merged, order = {}, []
        for uid, sec in want:
            if uid not in merged:
                merged[uid] = sec
                order.append(uid)
            elif not merged[uid] and sec:
                merged[uid] = sec

        filled = 0
        for uid in order:
            sec = merged[uid]
            try:
                prof = self.im.profile(uid=uid, sec_uid=sec, use_cache=False)
            except Exception:
                continue
            if prof.get("nickname"):
                self.idmap.cache_profile(uid, prof["nickname"],
                                         prof.get("sec_uid") or sec,
                                         prof.get("avatar") or "")
                self.nicknames[uid] = prof["nickname"]
                filled += 1
        log(f"昵称预取完成：候选 {len(order)} 个，补到昵称 {filled} 个")


# ---------------------------------------------------------------- 入口

def build_self_id(prefix_hint, dy_uid):
    """抖音 uid -> OneBot self_id。

    实测抖音 uid 约 3.6e15。加 7e18 后落在 7.003e18，int64 上限 9.22e18 之内，
    且和群号（实测 7.689e18）不同区间、和用户 uid 更不同区间。
    """
    if prefix_hint:
        return int(prefix_hint)
    return 7000000000000000000 + (int(dy_uid) % 1000000000000000)
