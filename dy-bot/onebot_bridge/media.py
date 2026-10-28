# -*- coding: utf-8 -*-
"""抖音加密图片 -> OneBot 可显示的 HTTP 媒体服务。

背景（全部实测，不是推断）
---------------------------------------------------------------
抖音图片消息（message_type 27）的 ``content.resource_url`` 里给的是
``origin_url_list`` / ``large_url_list`` 等 CDN 直链，但**下载下来不是图片**，
而是 AES-256-GCM 密文：

    ct  = GET origin_url_list[0]        # 372 字节，开头没有任何图片 magic
    key = bytes.fromhex(resource_url["skey"])      # 64 hex = 32 字节
    pt  = AESGCM(key).decrypt(ct[:12], ct[12:], None)   # 12 字节 nonce 前缀

解密结果 344 字节，md5 与 ``resource_url["md5"]`` / ``data_size`` 完全一致
（逐个 URL 变体都验过：origin 给原图、large/medium 给同一张缩略图、thumb 更小）。

所以 OneBot 侧以前拿到的是一串密文 URL —— 消息段本身没坏，是**内容**没人解。
直接在事件里塞 CDN 直链是不行的：客户端拿到的仍是密文。

本模块的做法
---------------------------------------------------------------
  * 事件里放的是**本桥自己的**短 URL：``{public_base}{path}/{token}``；
  * 客户端来取时，本桥才去下载 + 解密 + 落盘 + 带正确 Content-Type 回；
  * token 用 ``resource_url.md5``（抖音给的明文摘要，天然去重），
    没有 md5 时退化成 url 的 sha1；
  * 索引落盘（``index.json``），进程重启后旧 URL 仍然有效；
  * 图片按 ``ttl_sec`` / ``max_files`` 清理，不无限长胖。

事件构造路径上**不发任何网络请求**（register 是纯内存操作），
真正的下载只发生在 HTTP GET 那一下，且带超时。
"""
import hashlib
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .logutil import log

# 常见图片 magic -> (扩展名, Content-Type)
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
)
_WEBP_RIFF = b"RIFF"
_TOKEN_RE_OK = set("0123456789abcdef")


def sniff(data):
    """返回 (ext, content_type)；认不出来就给 (bin, application/octet-stream)。"""
    for magic, ext, ctype in _MAGIC:
        if data.startswith(magic):
            return ext, ctype
    if data[:4] == _WEBP_RIFF and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "bin", "application/octet-stream"


def _is_token(token):
    return bool(token) and 16 <= len(token) <= 64 and set(token) <= _TOKEN_RE_OK


def decrypt_if_needed(raw, skey):
    """按 skey 解 AES-256-GCM；没有 skey 或解失败就按明文处理。

    回来的是 (bytes, how)，how ∈ {"gcm", "plain"}。解出来的东西**必须**
    是图片才算数，否则宁可回退到明文（有些 CDN 变体本来就是明文）。
    """
    if skey:
        try:
            key = bytes.fromhex(str(skey).strip())
        except ValueError:
            key = b""
        if len(key) == 32 and len(raw) > 28:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                pt = AESGCM(key).decrypt(raw[:12], raw[12:], None)
                if sniff(pt)[0] != "bin":
                    return pt, "gcm"
            except Exception:
                pass
    return raw, "plain"


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def detect_public_host():
    """猜一个「Docker 容器能访问到宿主」的地址。

    AstrBot 跑在 bridge 网络的容器里，它要能取到本桥的图片。docker0 的
    网关地址（172.17.0.1）是容器一定能到的，拿它当默认值最稳。
    探测手法：UDP connect 不实际发包，只让内核选源地址。
    """
    for probe in ("172.17.0.1", "172.18.0.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 9))
                addr = s.getsockname()[0]
            finally:
                s.close()
            if addr and not addr.startswith("127."):
                return addr
        except Exception:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


class MediaCache:
    """图片密文索引 + 解密落盘 + 过期清理。线程安全。"""

    def __init__(self, conf=None):
        conf = conf or {}
        self.enabled = bool(conf.get("enabled", True))
        self.dir = Path(conf.get("dir")
                        or "/root/dyonebot/dy-bot/media_cache")
        self.path = (conf.get("path") or "/media").rstrip("/") or "/media"
        self.port = int(conf.get("port") or 6801)
        self.ttl = float(conf.get("ttl_sec") or 3600)
        self.max_files = int(conf.get("max_files") or 400)
        self.timeout = float(conf.get("fetch_timeout_sec") or 20)
        self.public_base = str(conf.get("public_base") or "").rstrip("/")
        if not self.public_base:
            host = conf.get("public_host") or detect_public_host()
            self.public_base = f"http://{host}:{self.port}"
        self._lock = threading.RLock()
        self._index = {}          # token -> {urls, skey, ext, ctype, ts}
        self._load()

    # ------------------------------------------------------------ 索引持久化
    @property
    def index_file(self):
        return self.dir / "index.json"

    def _load(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as err:
            log(f"媒体缓存目录创建失败 {self.dir}: {err}", "WARN")
        try:
            if self.index_file.exists():
                raw = json.loads(self.index_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._index = raw
        except Exception as err:
            log(f"媒体索引损坏，重建: {err}", "WARN")
            self._index = {}

    def _save_locked(self):
        try:
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.index_file)
        except Exception as err:
            log(f"媒体索引写盘失败: {err}", "WARN")

    # ------------------------------------------------------------ 注册
    def register(self, *, urls, skey="", md5="", data_size=0):
        """登记一条待取图片，返回 token（纯内存，可在事件循环里调用）。

        urls 里的第一个可下载直链会被记住；token 优先用抖音给的 md5，
        这样同一张图重复发也只存一份。
        """
        urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith("http")]
        if not urls or not self.enabled:
            return ""
        token = ""
        hint = str(md5 or "").strip().lower()
        if _is_token(hint):
            token = hint
        if not token:
            token = hashlib.sha1(urls[0].encode("utf-8")).hexdigest()[:40]
        with self._lock:
            item = self._index.get(token) or {}
            item.update({"urls": urls, "skey": str(skey or ""),
                         "md5": hint, "data_size": int(data_size or 0),
                         "ts": time.time()})
            self._index[token] = item
            self._save_locked()
        return token

    def url_for(self, token):
        if not token:
            return ""
        return f"{self.public_base}{self.path}/{token}"

    # ------------------------------------------------------------ 取内容
    def ensure(self, token):
        """保证 token 的明文图片在磁盘上，返回 (path, content_type)。

        同步阻塞（下载 + 解密），调用方负责丢线程池。
        """
        if not _is_token(token):
            return None, ""
        for ext, ctype in (("png", "image/png"), ("jpg", "image/jpeg"),
                           ("gif", "image/gif"), ("webp", "image/webp"),
                           ("bmp", "image/bmp")):
            f = self.dir / f"{token}.{ext}"
            if f.exists() and f.stat().st_size > 0:
                return f, ctype

        with self._lock:
            item = dict(self._index.get(token) or {})
        urls = item.get("urls") or []
        if not urls:
            return None, ""
        skey = item.get("skey") or ""
        want_md5 = (item.get("md5") or "").lower()
        last_err = ""
        for url in urls:
            try:
                raw = _fetch(url, self.timeout)
            except Exception as err:
                last_err = f"{type(err).__name__}: {err}"
                continue
            data, how = decrypt_if_needed(raw, skey)
            ext, ctype = sniff(data)
            if ext == "bin":
                last_err = f"解密结果不是图片（{how}, {len(data)}B）"
                continue
            got = hashlib.md5(data).hexdigest()
            if want_md5 and len(want_md5) == 32 and got != want_md5:
                # 摘要对不上说明拿错了副本（例如 thumb 被当成原图），继续换下一个
                last_err = f"md5 不符 got={got} want={want_md5}"
                continue
            f = self.dir / f"{token}.{ext}"
            try:
                tmp = f.with_name(f.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, f)
            except Exception as err:
                last_err = f"落盘失败: {err}"
                continue
            with self._lock:
                if token in self._index:
                    self._index[token].update({"ext": ext, "ctype": ctype,
                                               "got_md5": got, "how": how,
                                               "ts": time.time()})
                    self._save_locked()
            self.prune()
            return f, ctype
        log(f"图片解密失败 token={token[:12]}… {last_err}", "WARN")
        return None, ""

    # ------------------------------------------------------------ 清理
    def prune(self):
        """按 TTL + 数量上限清文件，索引同步收缩。"""
        try:
            with self._lock:
                now = time.time()
                drop = [t for t, it in self._index.items()
                        if self.ttl > 0 and now - float(it.get("ts") or 0) > self.ttl]
                for token in drop:
                    for f in self.dir.glob(f"{token}.*"):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    self._index.pop(token, None)
                files = sorted((f for f in self.dir.glob("*") if f.is_file()
                                and f.name != "index.json"),
                               key=lambda p: p.stat().st_mtime)
                if self.max_files > 0 and len(files) > self.max_files:
                    for f in files[:len(files) - self.max_files]:
                        try:
                            f.unlink()
                        except Exception:
                            pass
                if drop:
                    self._save_locked()
        except Exception as err:
            log(f"媒体清理异常: {type(err).__name__}: {err}", "WARN")

    # ------------------------------------------------------------ HTTP
    def routes(self, app, web):
        """把 GET {path}/{token} 挂到 aiohttp app 上。"""
        base = self.path

        async def handle(request):
            token = request.match_info.get("token") or ""
            token = token.rsplit(".", 1)[0] if "." in token else token
            if not _is_token(token):
                return web.Response(status=404, text="bad token")
            import asyncio
            try:
                path, ctype = await asyncio.to_thread(self.ensure, token)
            except Exception as err:
                log(f"媒体服务异常: {type(err).__name__}: {err}", "WARN")
                return web.Response(status=502, text="fetch failed")
            if path is None:
                return web.Response(status=404, text="not available")
            return web.FileResponse(path, headers={
                "Content-Type": ctype,
                "Cache-Control": "public, max-age=86400",
            })

        app.router.add_get(base + "/{token}", handle)
        app.router.add_get(base + "/{token}/", handle)
        return app


def public_base_of(cache):
    return cache.public_base if cache is not None else ""


__all__ = ["MediaCache", "sniff", "decrypt_if_needed", "detect_public_host",
           "public_base_of"]
