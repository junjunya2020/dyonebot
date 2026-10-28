# -*- coding: utf-8 -*-
"""WebUI 扫码登录：在面板里出二维码、看状态、把凭据写回 .env。

为什么不用 `login_qr.py`：那个脚本是「跑一次、命令行等」，二维码落成 PNG 文件
给外部页面轮询。面板要的是【在同一个进程里】拿到二维码内容并原地等结果，
所以这里把登录跑在一个后台线程里，二维码直接以 data URL 交给前端。

协议要点（都踩过）：
  * 二维码约 60 秒过期，上游会自动换新，`on_qrcode` 每次换码都会被调一次。
  * `qrcode_login` 是**阻塞**的，全程可能几分钟，必须丢线程。
  * 登录成功后才 save_credential 写 .env；写盘前要确认拿到的 auth 里
    真的有 sessionid，否则等于把空凭据覆盖掉好凭据（灾难）。
"""
import base64
import io
import threading
import time

from .logutil import log

STATE_IDLE = "idle"
STATE_WAITING = "waiting"       # 已出码，等人扫
STATE_SCANNED = "scanned"       # 已扫码，等手机确认
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

STATE_TEXT = {
    STATE_IDLE: "未开始",
    STATE_WAITING: "等待扫码",
    STATE_SCANNED: "已扫码，请在手机上确认",
    STATE_SUCCESS: "登录成功",
    STATE_FAILED: "登录失败",
    STATE_CANCELLED: "已取消",
}


def _qr_data_url(url):
    """二维码内容 -> PNG data URL（前端直接塞进 <img src>）。"""
    import qrcode
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class LoginManager:
    """一次只跑一个登录流；重复 start 复用当前流。"""

    def __init__(self, env_file="/root/dyonebot/douyin-spider/.env",
                 on_success=None, timeout=600, initial_uid=""):
        self.env_file = env_file
        self.on_success = on_success        # 登录成功后回调(auth)
        self.timeout = int(timeout)
        self._lock = threading.RLock()
        self._thread = None
        self._cancel = threading.Event()
        self._state = STATE_IDLE
        self._detail = ""
        self._qr = ""
        self._qr_count = 0
        self._started_at = 0.0
        self._finished_at = 0.0
        self._uid = str(initial_uid or "")
        self._creds = {}
        self._restore_saved_status()

    def _restore_saved_status(self):
        """Restore the persisted login summary after a process restart.

        The credentials themselves stay in .env; only their presence/count is
        exposed to the panel.  LoginManager used to reset this display to an
        empty idle state on every restart even when the bridge had loaded the
        credentials successfully.
        """
        creds, has_cookie = read_env_creds(self.env_file)
        if not has_cookie:
            return
        cookie_count = 0
        try:
            from dotenv import dotenv_values
            raw = str(dotenv_values(self.env_file).get("DY_COOKIES") or "")
            cookie_count = sum(1 for item in raw.split(";") if item.strip())
        except Exception:
            pass
        creds["cookie_count"] = cookie_count
        self._creds = creds
        self._state = STATE_SUCCESS
        self._detail = f"已从 {self.env_file} 读取持久化凭据"

    # ------------------------------------------------------------ 查询
    def status(self):
        with self._lock:
            return {
                "state": self._state,
                "state_text": STATE_TEXT.get(self._state, self._state),
                "detail": self._detail,
                "has_qr": bool(self._qr),
                "qr_count": self._qr_count,
                "elapsed_sec": round(time.time() - self._started_at, 1)
                               if self._started_at else 0.0,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "uid": self._uid,
                "creds": dict(self._creds),
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def qr(self):
        """只取二维码（单独接口，避免状态轮询每次都传一大坨 base64）。"""
        with self._lock:
            return {"qr": self._qr, "state": self._state, "qr_count": self._qr_count}

    # ------------------------------------------------------------ 控制
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "登录流程已在进行中"
            self._cancel.clear()
            self._state = STATE_WAITING
            self._detail = "正在获取二维码…"
            self._qr = ""
            self._qr_count = 0
            self._started_at = time.time()
            self._finished_at = 0.0
            self._uid = ""
            self._thread = threading.Thread(target=self._run, name="dy-webui-login",
                                            daemon=True)
            self._thread.start()
        log("WebUI 扫码登录：流程已启动")
        return True, "已启动"

    def cancel(self):
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return False, "没有正在进行的登录"
            self._cancel.set()
            self._state = STATE_CANCELLED
            self._detail = "用户取消"
            self._qr = ""
        log("WebUI 扫码登录：用户取消")
        return True, "已取消"

    # ------------------------------------------------------------ 执行
    def _run(self):
        try:
            from dy_apis.login_api import DYLoginApi
            api = DYLoginApi()
        except Exception as err:
            self._fail(f"初始化登录模块失败: {type(err).__name__}: {err}")
            return

        def on_qrcode(url):
            if self._cancel.is_set():
                raise RuntimeError("用户取消登录")
            with self._lock:
                self._qr_count += 1
                if self._qr_count > 1:
                    self._state = STATE_WAITING
                self._detail = (f"第 {self._qr_count} 张二维码"
                                f"（约 60 秒过期，自动换新）")
            try:
                data_url = _qr_data_url(url)
            except Exception as err:
                log(f"二维码渲染失败: {type(err).__name__}: {err}", "WARN")
                return
            with self._lock:
                self._qr = data_url
            log(f"WebUI 扫码登录：第 {self._qr_count} 张二维码已生成")

        try:
            auth = api.qrcode_login(timeout=self.timeout, show_qr=False,
                                    on_qrcode=on_qrcode)
        except Exception as err:
            if self._cancel.is_set():
                with self._lock:
                    self._state = STATE_CANCELLED
                    self._detail = "已取消"
                    self._qr = ""
                    self._finished_at = time.time()
                return
            self._fail(f"{type(err).__name__}: {err}")
            return

        # 关键防线：没有 sessionid 就绝不写盘，否则会用好凭据换一份空的
        cookie = getattr(auth, "cookie", None) or {}
        if not cookie.get("sessionid"):
            self._fail("登录返回的凭据里没有 sessionid，已放弃写盘（保护原有登录态）")
            return

        try:
            path = api.save_credential(auth)
        except Exception as err:
            self._fail(f"凭据写盘失败: {type(err).__name__}: {err}")
            return

        creds = {
            "sessionid": bool(cookie.get("sessionid")),
            "ticket": bool(getattr(auth, "ticket", None)),
            "ts_sign": bool(getattr(auth, "ts_sign", None)),
            "client_cert": bool(getattr(auth, "client_cert", None)),
            "private_key": bool(getattr(auth, "private_key", None)),
            "dtrait_blob": bool(getattr(auth, "dtrait_blob", None)),
            "cookie_count": len(cookie),
        }
        uid = ""
        try:
            uid = str(auth.get_uid())
        except Exception:
            uid = ""

        with self._lock:
            self._state = STATE_SUCCESS
            self._detail = f"凭据已写入 {path}"
            self._uid = uid
            self._creds = creds
            self._finished_at = time.time()
            self._qr = ""
        log(f"WebUI 扫码登录成功 uid={uid or '?'} creds={creds}")

        if self.on_success is not None:
            try:
                self.on_success(auth)
            except Exception as err:
                log(f"登录成功回调失败: {type(err).__name__}: {err}", "WARN")

    def _fail(self, detail):
        with self._lock:
            self._state = STATE_FAILED
            self._detail = detail
            self._finished_at = time.time()
            if not self._creds:
                self._qr = ""
        log(f"WebUI 扫码登录失败：{detail}", "ERROR")


def read_env_creds(env_file="/root/dyonebot/douyin-spider/.env"):
    """读 .env 里六件套的【有无】状态，不返回凭据内容本身。"""
    want = ("DY_COOKIES", "DY_TICKET", "DY_TS_SIGN", "DY_CLIENT_CERT",
            "DY_PRIVATE_KEY", "DY_DTRAIT_BLOB", "DY_SESSION_DTRAIT")
    out = {k: False for k in want}
    cookie_value = ""
    try:
        for line in open(env_file, encoding="utf-8"):
            name, sep, value = line.strip().partition("=")
            if not sep or name not in out:
                continue
            value = value.strip().strip("'\"")
            out[name] = bool(value)
            if name == "DY_COOKIES":
                cookie_value = value
    except OSError:
        return out, False
    return out, bool(cookie_value)
