# -*- coding: utf-8 -*-
"""抖音推送 WS 看护：多级自动重连 + 状态快照（给 WebUI 看）。

背景（全部实测，不是推断）
---------------------------------------------------------------
上游 `dy_apis/douyin_recv_msg.py` 自带一个重连循环，但它有两个硬伤：

  1. `on_error` 里只认 `ConnectionRefusedError` 和
     `WebSocketConnectionClosedException` 两种异常才会置 `_should_reconnect`。
     而 WebSocketApp 的 ping 超时抛的是 **`WebSocketTimeoutException`**
     （`WebSocketTimeoutException` 与 `WebSocketConnectionClosedException` 是
     兄弟类，不是子类 —— 已用 MRO 验证）。于是「心跳超时」这条最常见的掉线
     路径上 `_should_reconnect` 永远是 False，`start()` 直接 break：
     **掉线后永久静默**，日志里只剩 `### closed ###`，再也不会重连。
  2. `WebSocketApp.run_forever` 的 `ping_interval` 默认 0（不发心跳），
     连接会被服务端当闲置回收（实测约 30~60 秒）。

所以这里**不再依赖上游的循环**：上游只负责「建立一条连接 + 收帧」，
重连策略由本模块自己拥有（`auto_reconnect=False`，一次 start() = 一次尝试）。

三级重试（用户要求：先直接重连，不行就换 token 重启，前端要能看到状态）
---------------------------------------------------------------
  1 级 直连重连   同一套凭据重新建连（最常见的网络抖动/被踢，这一步就够）
  2 级 换 token   重新读 .env 登录态 + 刷新 msToken + 重建设备指纹，
                  再重连（应对 session/票据中途失效）
  3 级 需登录     连续失败且换 token 也救不回来 -> 状态置 needs_login，
                  前端亮红灯提示扫码；等 WebUI 登录成功后自动回到 1 级

只要有一次连接「稳定存活」超过 `stable_sec`，失败计数清零、级别回到 1 级，
避免偶发抖动把级别一路推到要重新登录。
"""
import json
import threading
import time
import traceback

LEVEL_DIRECT = 1
LEVEL_TOKEN = 2
LEVEL_LOGIN = 3

LEVEL_NAME = {
    LEVEL_DIRECT: "1级·直连重连",
    LEVEL_TOKEN: "2级·换token重连",
    LEVEL_LOGIN: "3级·需重新登录",
}

STATE_STARTING = "starting"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_NEEDS_LOGIN = "needs_login"
STATE_STOPPED = "stopped"

STATE_TEXT = {
    STATE_STARTING: "启动中",
    STATE_CONNECTING: "连接中",
    STATE_CONNECTED: "已连接",
    STATE_RECONNECTING: "重连中",
    STATE_NEEDS_LOGIN: "需要重新登录",
    STATE_STOPPED: "已停止",
}


def _now():
    return time.time()


def log(msg, level="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] {level} {msg}", flush=True)


class UpstreamSupervisor:
    """常驻线程跑抖音推送 WS，断线按三级策略自愈。

    只暴露四件事给外部：start / stop / status / 三个手动触发。
    所有状态字段都是普通标量，`status()` 返回的是【自己构造的新 dict】，
    不是内部对象的引用。
    """

    def __init__(self, auth, config, on_push, auth_loader=None):
        """
        :param auth:        初始 DouyinAuth
        :param config:      bridge 的完整 config（取 douyin_ws 段）
        :param on_push:     收到推送帧时调用，签名 on_push(raw_bytes)，
                            在【看护线程】里执行，调用方自己负责切回事件循环
        :param auth_loader: 可选，无参可调用，返回新的 DouyinAuth（2 级重试用）
        """
        self.auth = auth
        self.config = config or {}
        self.on_push = on_push
        self.auth_loader = auth_loader

        conf = self.config.get("douyin_ws") or {}
        self.ping_sec = float(conf.get("ping_interval_sec", 25) or 0)
        self.backoff_min = max(0.5, float(conf.get("reconnect_min_sec", 2) or 2))
        self.backoff_max = max(self.backoff_min,
                               float(conf.get("reconnect_max_sec", 60) or 60))
        self.direct_retries = max(1, int(conf.get("direct_retries", 5) or 5))
        self.token_retries = max(0, int(conf.get("token_retries", 3) or 3))
        self.stable_sec = max(5.0, float(conf.get("stable_sec", 60) or 60))
        self.mssdk_heartbeat_sec = max(
            0.0, float(conf.get("mssdk_heartbeat_sec", 300) or 0)
        )

        self._lock = threading.RLock()
        self._thread = None
        self._mssdk_thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()        # 手动触发（强制重连/换token）
        self._reload_token = False
        self._recv = None

        self._state = STATE_STARTING
        self._level = LEVEL_DIRECT
        self._failures = 0
        self._connected_at = 0.0
        self._last_open_at = 0.0
        self._last_close_at = 0.0
        self._last_error = ""
        self._next_retry_at = 0.0
        self._connects = 0
        self._disconnects = 0
        self._reconnects = 0
        self._token_reloads = 0
        self._last_reload_at = 0.0
        self._last_mssdk_heartbeat_at = 0.0
        self._mssdk_heartbeat_failures = 0
        self._ws_host = ""

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dy-upstream-ws",
                                        daemon=True)
        self._thread.start()
        if self.mssdk_heartbeat_sec > 0:
            self._mssdk_thread = threading.Thread(
                target=self._mssdk_heartbeat_loop,
                name="dy-mssdk-heartbeat", daemon=True)
            self._mssdk_thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        recv = self._recv
        if recv is not None:
            try:
                recv.stop()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
        mt = self._mssdk_thread
        if mt and mt.is_alive() and mt is not threading.current_thread():
            mt.join(timeout=min(timeout, 2.0))
        self._set(state=STATE_STOPPED)

    # ------------------------------------------------------------ 手动触发
    def force_reconnect(self, reason="手动重连"):
        """外部（WebUI）要求立刻重连一次：打断当前连接并重置级别。"""
        with self._lock:
            self._failures = 0
            self._level = LEVEL_DIRECT
            self._last_error = f"手动触发: {reason}"
        log(f"上游 WS 收到手动重连请求（{reason}），立即重连")
        recv = self._recv
        if recv is not None:
            try:
                recv.stop()          # 让 run_forever 返回，看护线程进入下一轮
            except Exception:
                pass
        self._wake.set()

    def request_token_reload(self, reason="登录态已更新"):
        """外部（WebUI 扫码成功）通知：换凭据重连。"""
        with self._lock:
            self._reload_token = True
            self._failures = 0
            self._level = LEVEL_DIRECT
        log(f"上游 WS 收到凭据更新通知（{reason}），将换 token 重连")
        recv = self._recv
        if recv is not None:
            try:
                recv.stop()
            except Exception:
                pass
        self._wake.set()

    # ------------------------------------------------------------ 状态快照
    def status(self):
        with self._lock:
            now = _now()
            uptime = (now - self._connected_at) if self._connected_at else 0.0
            return {
                "state": self._state,
                "state_text": STATE_TEXT.get(self._state, self._state),
                "connected": self._state == STATE_CONNECTED,
                "level": self._level,
                "level_name": LEVEL_NAME.get(self._level, str(self._level)),
                "failures": self._failures,
                "uptime_sec": round(uptime, 1),
                "last_open_at": self._last_open_at,
                "last_close_at": self._last_close_at,
                "last_error": self._last_error,
                "next_retry_in": max(0.0, round(self._next_retry_at - now, 1))
                                 if self._next_retry_at else 0.0,
                "connects": self._connects,
                "disconnects": self._disconnects,
                "reconnects": self._reconnects,
                "token_reloads": self._token_reloads,
                "last_reload_at": self._last_reload_at,
                "ws_host": self._ws_host,
                "ping_interval_sec": self.ping_sec,
                "direct_retries": self.direct_retries,
                "token_retries": self.token_retries,
                "stable_sec": self.stable_sec,
                "mssdk_heartbeat_sec": self.mssdk_heartbeat_sec,
                "last_mssdk_heartbeat_at": self._last_mssdk_heartbeat_at,
                "mssdk_heartbeat_failures": self._mssdk_heartbeat_failures,
            }

    def _mssdk_heartbeat_loop(self):
        """Keep the browser-like mssdk session alive independently of WS ping."""
        if self._stop.wait(min(10.0, self.mssdk_heartbeat_sec)):
            return
        while not self._stop.is_set():
            with self._lock:
                auth = self.auth
            try:
                token = auth.refresh_mstoken(common=True, common_behavior=True)
                persisted = False
                if token and (getattr(auth, "cookie", None) or {}).get("sessionid"):
                    try:
                        # mssdk may rotate auxiliary session cookies.  Keep the
                        # durable file in sync so the next bridge restart does
                        # not resurrect an older HTTP session.
                        from dy_apis.login_api import DYLoginApi
                        DYLoginApi().save_credential(auth)
                        persisted = True
                    except Exception as err:
                        log(f"上游 mssdk 保活凭据落盘失败: {type(err).__name__}: {err}",
                            "WARN")
                with self._lock:
                    self._last_mssdk_heartbeat_at = _now()
                    if token:
                        self._mssdk_heartbeat_failures = 0
                    else:
                        self._mssdk_heartbeat_failures += 1
                if token:
                    log("上游 mssdk 会话保活成功，msToken 已轮换"
                        + ("，凭据已持久化" if persisted else ""))
                else:
                    log("上游 mssdk 会话保活未返回新 token", "WARN")
            except Exception as err:
                with self._lock:
                    self._last_mssdk_heartbeat_at = _now()
                    self._mssdk_heartbeat_failures += 1
                log(f"上游 mssdk 会话保活失败: {type(err).__name__}: {err}",
                    "WARN")
            if self._stop.wait(self.mssdk_heartbeat_sec):
                return

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    # ------------------------------------------------------------ 主循环
    def _run(self):
        log("上游 WS 看护启动"
            f"（心跳 {self.ping_sec:.0f}s，1级直连 {self.direct_retries} 次，"
            f"2级换token {self.token_retries} 次）")
        while not self._stop.is_set():
            try:
                opened, last_err, run_sec = self._attempt()
            except Exception as err:
                opened, run_sec = False, 0.0
                last_err = f"{type(err).__name__}: {err}"
                log("上游 WS 尝试异常:\n" + traceback.format_exc(), "ERROR")

            if self._stop.is_set():
                break

            with self._lock:
                self._disconnects += 1
                self._last_close_at = _now()
                # 稳定存活过，说明不是持续性故障：不算一次失败，级别回落到 1 级，
                # 免得偶发抖动把级别一路推到「要重新扫码」。
                stable = run_sec >= self.stable_sec
                if stable:
                    self._failures = 0
                else:
                    self._failures += 1
                failures = self._failures
                level_before = self._level
                if stable and level_before != LEVEL_DIRECT:
                    self._level = LEVEL_DIRECT

            if stable and level_before != LEVEL_DIRECT:
                log(f"上游 WS 稳定存活 {run_sec:.0f}s，重试级别回落到 "
                    f"{LEVEL_NAME[LEVEL_DIRECT]}")

            if last_err:
                with self._lock:
                    self._last_error = last_err

            if self._wake.is_set():
                # 手动触发优先：不等待退避，直接进下一轮
                self._wake.clear()
                with self._lock:
                    self._failures = 0
                    self._level = LEVEL_DIRECT
                    want_reload = self._reload_token
                    self._reload_token = False
                if want_reload:
                    # 面板「换 token 重连」必须真的换凭据：重读 .env + 刷 msToken。
                    # 这个标志以前只写不读，于是按钮名叫换 token、行为却等同普通
                    # 重连（token 没换，等于白点）。
                    log("上游 WS 立即重连（手动触发·换 token）")
                    self._reload_credentials()
                else:
                    log("上游 WS 立即重连（手动触发）")
                continue

            level = self._decide_level(failures)
            self._set(level=level)

            if level == LEVEL_LOGIN:
                self._set(state=STATE_NEEDS_LOGIN)
                log("上游 WS 连续重连失败且换 token 无效，"
                    "需要在 WebUI 重新扫码登录（登录成功后会自动重连）", "ERROR")
                self._wait_login()
                continue

            if level == LEVEL_TOKEN:
                self._set(state=STATE_RECONNECTING)
                self._reload_credentials()

            delay = min(self.backoff_min * (2 ** max(0, failures - 1)),
                        self.backoff_max)
            self._set(state=STATE_RECONNECTING,
                      next_retry_at=_now() + delay)
            log(f"上游 WS 掉线（{last_err or '连接结束'}），"
                f"{LEVEL_NAME[level]}，{delay:.0f} 秒后重连"
                f"（连续失败 {failures} 次）", "WARN")
            self._wait(delay)

        self._set(state=STATE_STOPPED, connected_at=0.0)
        log("上游 WS 看护退出")

    def _decide_level(self, failures):
        if failures <= self.direct_retries:
            return LEVEL_DIRECT
        if failures <= self.direct_retries + self.token_retries:
            return LEVEL_TOKEN
        return LEVEL_LOGIN

    def _wait(self, delay):
        """可被打断的退避等待（手动触发/停止时立刻醒）。"""
        end = _now() + max(0.0, delay)
        while not self._stop.is_set() and _now() < end:
            if self._wake.is_set():
                return
            time.sleep(min(0.25, max(0.05, end - _now())))

    def _wait_login(self):
        """3 级：挂着等外部（WebUI 登录）唤醒，不烧 CPU。"""
        self._set(next_retry_at=0.0)
        while not self._stop.is_set():
            if self._wake.is_set():
                self._wake.clear()
                with self._lock:
                    self._failures = 0
                    self._level = LEVEL_DIRECT
                log("上游 WS 收到唤醒（登录态已更新），重新开始连接")
                return
            self._reload_token = False
            self._wake.wait(1.0)

    # ------------------------------------------------------------ 凭据换新
    def _reload_credentials(self):
        """2 级：重新读 .env 登录态 + 刷新 msToken。"""
        with self._lock:
            self._token_reloads += 1
            self._last_reload_at = _now()
        try:
            if self.auth_loader is not None:
                fresh = self.auth_loader()
                if fresh is not None:
                    self.auth = fresh
                    log("上游 WS 2级重试：已重新读取 .env 登录态")
        except Exception as err:
            log(f"上游 WS 重读登录态失败: {type(err).__name__}: {err}", "WARN")
        try:
            token = self.auth.refresh_mstoken()
            log(f"上游 WS 2级重试：msToken 已刷新 ({'ok' if token else '空'})")
        except Exception as err:
            log(f"上游 WS 刷新 msToken 失败: {type(err).__name__}: {err}", "WARN")

    # ------------------------------------------------------------ 单次连接
    def _build_recv(self):
        from dy_apis.douyin_recv_msg import DouyinRecvMsg

        recv = DouyinRecvMsg(self.auth, auto_reconnect=False)
        try:
            self._ws_host = str(recv.url).split("?", 1)[0]
        except Exception:
            self._ws_host = ""

        # 心跳：run_forever 默认 ping_interval=0。这里按实例包一层补默认值，
        # 并且【不覆盖】上游 on_error 的语义（我们自己看护重连）。
        # 用 getattr 取 _new_ws：上游换了实现/测试桩没有这个钩子时，退化成
        # 「不补心跳」而不是整条连接直接抛 AttributeError。
        ping_sec = self.ping_sec
        orig_new_ws = getattr(recv, "_new_ws", None)
        if callable(orig_new_ws) and ping_sec > 0:
            def _new_ws_with_ping():
                ws = orig_new_ws()
                orig_run_forever = ws.run_forever

                def run_forever(*args, **kwargs):
                    kwargs.setdefault("ping_interval", ping_sec)
                    kwargs.setdefault("ping_timeout", max(5.0, ping_sec / 2))
                    return orig_run_forever(*args, **kwargs)

                ws.run_forever = run_forever
                return ws

            recv._new_ws = _new_ws_with_ping
        elif not callable(orig_new_ws):
            log("上游 DouyinRecvMsg 没有 _new_ws 钩子，本次连接不发心跳", "WARN")

        mark = {"opened": False, "err": "", "opened_at": 0.0}
        upstream_open = recv.on_open
        upstream_error = recv.on_error
        upstream_close = recv.on_close
        on_push = self.on_push

        def on_open(ws):
            try:
                upstream_open(ws)
            except Exception as err:
                log(f"上游 on_open 异常: {type(err).__name__}: {err}", "WARN")
            mark["opened"] = True
            mark["opened_at"] = _now()
            with self._lock:
                self._connects += 1
                self._connected_at = _now()
                self._last_open_at = _now()
                self._state = STATE_CONNECTED
                if self._connects > 1:
                    self._reconnects += 1
            log("抖音推送 WS 已连接")

        def on_message(_ws, message):
            try:
                on_push(message)
            except Exception as err:
                log(f"推送投递失败: {type(err).__name__}: {err}", "WARN")

        def on_error(ws, err):
            mark["err"] = f"{type(err).__name__}: {err}"
            try:
                upstream_error(ws, err)
            except Exception as e2:
                log(f"上游 on_error 异常: {type(e2).__name__}: {e2}", "WARN")

        def on_close(ws, code, msg):
            try:
                upstream_close(ws, code, msg)
            except Exception as e2:
                log(f"上游 on_close 异常: {type(e2).__name__}: {e2}", "WARN")

        recv.on_open = on_open
        recv.on_message = on_message
        recv.on_error = on_error
        recv.on_close = on_close
        return recv, mark

    def _attempt(self):
        """跑一条连接，返回 (是否连上过, 最后一次错误, 存活秒数)。

        上游 start(auto_reconnect=False) 只会尝试一条连接：run_forever 返回后
        循环即 break。所以这里能安全地把它当成「一次尝试」。
        """
        self._set(state=STATE_CONNECTING, next_retry_at=0.0)
        recv, mark = self._build_recv()
        self._recv = recv
        try:
            recv.start()
        finally:
            self._recv = None

        run_sec = (_now() - mark["opened_at"]) if mark["opened"] else 0.0
        with self._lock:
            self._connected_at = 0.0
        return mark["opened"], mark["err"], run_sec


# ---------------------------------------------------------------- 工具

def reload_auth_from_env(cookie_getter=None):
    """重新按 .env 构造 DouyinAuth（2 级重试用）。

    只读 .env，绝不触发扫码/短信登录 —— 桥是后台进程，不能自己去抢二维码。
    `.env` 里有有效 DY_COOKIES 才返回 Auth，否则返回 None。
    """
    import os
    env_file = os.environ.get("DYBOT_DY_ENV",
                              "/root/dyonebot/douyin-spider/.env")
    values = {}
    try:
        from dotenv import dotenv_values
        values = {str(k): str(v or "") for k, v in
                  dotenv_values(env_file).items() if k}
    except OSError:
        return None
    raw = values.get("DY_COOKIES", "")
    if not re_has_session(raw):
        return None
    from builder.auth import DouyinAuth
    try:
        return DouyinAuth.from_cookie(
            raw,
            ticket=values.get("DY_TICKET") or None,
            ts_sign=values.get("DY_TS_SIGN") or None,
            client_cert=values.get("DY_CLIENT_CERT") or None,
            private_key=values.get("DY_PRIVATE_KEY") or None,
            dtrait_blob=values.get("DY_DTRAIT_BLOB") or None,
            session_dtrait=values.get("DY_SESSION_DTRAIT") or None,
            bootstrap_creator=False,
        )
    except Exception as err:
        log(f"按 .env 重建登录态失败: {type(err).__name__}: {err}", "WARN")
        return None


def re_has_session(cookie_str):
    """粗略判断 cookie 串里有没有 sessionid（别把空串当登录态）。"""
    if not cookie_str:
        return False
    for token in str(cookie_str).split(";"):
        name, sep, value = token.strip().partition("=")
        if sep and name.strip() == "sessionid" and value.strip():
            return True
    return False


def snapshot_json(status):
    """状态快照 -> 可直接塞进 JSON 响应的纯数据（不引用任何活对象）。"""
    return json.loads(json.dumps(status, ensure_ascii=False))
