# -*- coding: utf-8 -*-
"""进程日志中枢：stdout/stderr + logging 三路汇进同一个环形缓冲。

为什么要单独一层：

  1. WebUI 要能看日志。而日志有三个来源 —— 我们自己的 log()、上游
     douyin-spider 的裸 print()（"WebSocket connection open." / "### error ###"）、
     以及 websockets / asyncio 的 logging。三路都得进同一个面板，否则
     「上游为什么掉线」这种问题在面板上根本看不见。
  2. 所以这里不搞「谁调用谁记录」，而是把进程 stdout/stderr 整体接管（Tee），
     凡是写出来的行都进 环形缓冲 + 落盘 + 推给 SSE 订阅者。

单一事实来源 = 进程输出流；文件、内存、实时流三者内容必然一致。

用法：
    hub = logutil.setup("/path/bridge.log")   # 进程启动时调一次
    logutil.log("桥已启动")                    # 等价 print，交给 Tee 收
    hub.tail(200)                              # WebUI 拉历史
    q = hub.subscribe()                        # WebUI 拉实时流
"""
import asyncio
import collections
import logging
import os
import re
import sys
import threading
import time

# 我们自己 log() 打出来的行：[HH:MM:SS] LEVEL msg
_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+([A-Z]+)\s+(.*)$")

HUB = None

_LEVEL_ORDER = {"TRACE": 5, "DEBUG": 10, "OUT": 15, "INFO": 20, "WARN": 30,
                "WARNING": 30, "ERR": 35, "ERROR": 40, "CRITICAL": 50}


def log(msg, level="INFO"):
    """唯一日志入口：打印一行。

    注意这里【只打印】。记录/落盘/分发都在 Tee 里做 —— 只有一条路径，
    才不会出现「面板少了某一行」这种对不上的情况。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {level} {msg}", flush=True)


class LogHub:
    """环形缓冲 + 文件 + 实时订阅者。线程安全。"""

    def __init__(self, path=None, capacity=3000, max_bytes=20 * 1024 * 1024):
        self.path = str(path) if path else ""
        self._buf = collections.deque(maxlen=max(100, int(capacity)))
        self._lock = threading.RLock()
        self._seq = 0
        self._total = 0
        self._max_bytes = int(max_bytes or 0)
        self._fh = None
        self._loop = None
        self._subs = set()
        self._open_file()

    # ------------------------------------------------------------ 文件
    def _open_file(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        except Exception as err:
            self._fh = None
            log(f"日志文件打开失败 {self.path}: {type(err).__name__}: {err}",
                "WARN")

    def _rotate_if_needed(self):
        fh = self._fh
        if not fh or self._max_bytes <= 0:
            return
        if self._seq % 200:
            return
        try:
            if fh.tell() < self._max_bytes:
                return
        except Exception:
            return
        try:
            fh.close()
        except Exception:
            pass
        try:
            os.replace(self.path, self.path + ".1")
        except Exception:
            pass
        self._open_file()

    # ------------------------------------------------------------ 记录
    def record(self, level, msg, time_text=None, ts=None):
        level = (level or "INFO").upper()
        if level == "WARNING":
            level = "WARN"
        with self._lock:
            self._seq += 1
            self._total += 1
            rec = {
                "seq": self._seq,
                "ts": float(ts if ts is not None else time.time()),
                "time": time_text or time.strftime("%H:%M:%S"),
                "level": level,
                "msg": str(msg),
            }
            self._buf.append(rec)
            fh = self._fh
            if fh:
                try:
                    fh.write(f"[{rec['time']}] {rec['level']} {rec['msg']}\n")
                    fh.flush()
                except Exception:
                    pass
            self._rotate_if_needed()
        self._publish(rec)
        return rec

    def emit_line(self, line, tag="out"):
        """接一行原始进程输出。属于我们的格式就解析出 level，否则原样记。"""
        line = str(line).rstrip("\r")
        if not line:
            return
        m = _LINE_RE.match(line)
        if m:
            self.record(m.group(2), m.group(3), time_text=m.group(1))
            return
        self.record("ERR" if tag == "err" else "OUT", line)

    # ------------------------------------------------------------ 查询
    def tail(self, limit=300, since_seq=None, level=None):
        limit = max(1, min(int(limit or 300), self._buf.maxlen))
        with self._lock:
            rows = list(self._buf)
        if since_seq is not None:
            rows = [r for r in rows if r["seq"] > int(since_seq)]
        if level:
            want = _LEVEL_ORDER.get(str(level).upper(), 0)
            rows = [r for r in rows
                    if _LEVEL_ORDER.get(r["level"], 20) >= want]
        return rows[-limit:]

    def stats(self):
        with self._lock:
            return {"path": self.path, "total": self._total,
                    "last_seq": self._seq, "capacity": self._buf.maxlen,
                    "file_exists": bool(self.path and os.path.exists(self.path))}

    # ------------------------------------------------------------ 实时订阅
    def bind_loop(self, loop):
        self._loop = loop

    def subscribe(self, maxsize=1000):
        q = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)

    def _publish(self, rec):
        if not self._subs:
            return
        for q in list(self._subs):
            loop = self._loop
            try:
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._put, q, rec)
                else:
                    self._put(q, rec)
            except Exception:
                pass

    @staticmethod
    def _put(q, rec):
        try:
            q.put_nowait(rec)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(rec)
            except Exception:
                pass

    def close(self):
        with self._lock:
            fh, self._fh = self._fh, None
        if fh:
            try:
                fh.close()
            except Exception:
                pass


class _Tee:
    """把 stdout/stderr 抄一份进 LogHub，同时照常写回原流。

    需要伪装得够像一个文件对象：有些库会摸 isatty()/fileno()/encoding。
    """

    def __init__(self, stream, hub, tag):
        self._stream = stream
        self._hub = hub
        self._tag = tag
        self._buf = ""
        self._lock = threading.Lock()

    # ---- 写
    def write(self, data):
        if not isinstance(data, str):
            try:
                data = data.decode("utf-8", "replace")
            except Exception:
                data = str(data)
        try:
            self._stream.write(data)
            self._stream.flush()
        except Exception:
            pass
        with self._lock:
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._hub.emit_line(line, self._tag)
        return len(data)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def isatty(self):
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def close(self):        # 不真的关掉进程原流
        self.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _HubHandler(logging.Handler):
    """logging -> LogHub。"""

    def __init__(self, hub):
        logging.Handler.__init__(self)
        self.hub = hub

    def emit(self, record):
        try:
            self.hub.record(record.levelname, self.format(record))
        except Exception:
            pass


_installed = {"done": False}


def setup(path=None, capacity=3000, max_bytes=20 * 1024 * 1024,
          level=logging.INFO):
    """安装日志中枢。进程启动时调一次，重复调用只更新文件句柄。"""
    global HUB
    if _installed["done"] and HUB is not None:
        return HUB
    HUB = LogHub(path=path, capacity=capacity, max_bytes=max_bytes)
    try:
        sys.stdout = _Tee(getattr(sys, "__stdout__", None) or sys.stdout,
                          HUB, "out")
        sys.stderr = _Tee(getattr(sys, "__stderr__", None) or sys.stderr,
                          HUB, "err")
    except Exception:
        pass
    try:
        root = logging.getLogger()
        root.addHandler(_HubHandler(HUB))
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
    except Exception:
        pass
    _installed["done"] = True
    return HUB


def get_hub():
    return HUB
