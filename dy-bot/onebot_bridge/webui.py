# -*- coding: utf-8 -*-
"""dyonebot 控制面板：登录 / 上游状态 / 日志 / 配置。

鉴权设计（用户明确要求：面板 token 和 WS token 必须分开）
---------------------------------------------------------------
  * 面板用 `webui.token`（环境变量 DYBOT_WEBUI_TOKEN）—— 只进浏览器。
  * OneBot 的 WS/HTTP 用「ws token」（环境变量 DYBOT_WS_TOKEN）—— 只给
    AstrBot / NapCat 这类客户端。
  两者是**两套独立凭据**，任何一边泄露都不会让对方失守；面板 token 绝不能
  拿去当 WS token 用（所以面板里也不回显 WS token 的明文）。

路由
---------------------------------------------------------------
  GET  /                        面板页面（内嵌 JS，无外部依赖）
  GET  /login                   扫码登录页
  GET  /api/status              整体状态（上游 WS / OneBot 连接 / 登录 / 计数）
  GET  /api/upstream            上游 WS 状态（前端轮询用，纯数据）
  GET  /api/logs?limit=&level=&since=   历史日志
  GET  /api/logs/stream         SSE 实时日志
  GET  /api/login/status        扫码登录状态
  GET  /api/login/qr            二维码（单独拉，不掺进状态轮询）
  POST /api/login/start         开始扫码登录
  POST /api/login/cancel        取消
  POST /api/upstream/reconnect  立即重连上游 WS
  POST /api/upstream/reload-token  换 token 重连
  GET  /api/config              当前配置（凭据字段只回显有无）

所有 /api/* 都要 token；token 校验通过后下发一枚 HttpOnly cookie，
页面上就不必反复带 token（浏览器直接刷新也能继续用）。
"""
import asyncio
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

from aiohttp import web

from .logutil import log

COOKIE_NAME = "dybot_token"


def _mask(value, keep=4):
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


class WebUI:
    """面板服务。持有的是引用，构造时不启网络。"""

    def __init__(self, config, bridge, supervisor=None, login_manager=None,
                 hub=None):
        self.config = config or {}
        self.bridge = bridge
        self.supervisor = supervisor
        self.login = login_manager
        self.hub = hub
        conf = self.config.get("webui") or {}
        # host 允许三种写法：
        #   "127.0.0.1"                单个地址
        #   ["127.0.0.1", "10.0.0.5"]  只监听这几个地址（推荐：要开局域网就
        #                              写本机网卡 IP，别写 0.0.0.0，免得
        #                              Docker 网桥 172.x 也一并被监听）
        #   "0.0.0.0"                  所有网卡
        raw_host = conf.get("host", "127.0.0.1")
        if isinstance(raw_host, (list, tuple)):
            self.hosts = [str(h).strip() for h in raw_host if str(h).strip()]
        else:
            self.hosts = [str(raw_host).strip() or "127.0.0.1"]
        if not self.hosts:
            self.hosts = ["127.0.0.1"]
        self.host = self.hosts[0]          # 兼容旧代码/面板回显
        self.port = int(conf.get("port", 6800))
        self.token = str(conf.get("token") or "")
        self.title = conf.get("title") or "dyonebot 控制面板"
        self.started_at = time.time()
        self._app = None
        self._runner = None

    # ------------------------------------------------------------ 启动
    async def serve(self):
        app = web.Application(middlewares=[self._auth_mw])
        app.add_routes([
            web.get("/", self.page_index),
            web.get("/login", self.page_login),
            web.get("/api/status", self.api_status),
            web.get("/api/upstream", self.api_upstream),
            web.get("/api/logs", self.api_logs),
            web.get("/api/logs/stream", self.api_logs_stream),
            web.get("/api/login/status", self.api_login_status),
            web.get("/api/login/qr", self.api_login_qr),
            web.post("/api/login/start", self.api_login_start),
            web.post("/api/login/cancel", self.api_login_cancel),
            web.post("/api/upstream/reconnect", self.api_upstream_reconnect),
            web.post("/api/upstream/reload-token", self.api_upstream_reload),
            web.get("/api/config", self.api_config),
            web.get("/healthz", self.api_healthz),
        ])
        self._app = app
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._sites = []
        bound = []
        for bind_host in self.hosts:
            site = web.TCPSite(self._runner, bind_host, self.port)
            try:
                await site.start()
            except OSError as err:
                log(f"面板监听 {bind_host}:{self.port} 绑定失败（{err}）", "WARN")
                continue
            self._sites.append(site)
            bound.append(bind_host)
        if not self._sites:
            log(f"面板端口 {self.port} 全部绑定失败，面板不可用", "WARN")
            return
        shown = "、".join(f"http://{h}:{self.port}/" for h in bound)
        if self.token:
            log(f"控制面板已启动 {shown} "
                f"（token 已启用，与 WS token 独立）")
        else:
            log(f"控制面板已启动 {shown} "
                f"（未设 token，仅建议本机使用）", "WARN")

    async def stop(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------ 鉴权
    @web.middleware
    async def _auth_mw(self, request, handler):
        path = request.path
        if path == "/healthz":
            return await handler(request)
        if not self.token:
            # 未设 token：允许直连（本机面板），但仍旧拒绝带错 token 的请求
            return await handler(request)

        given = (request.headers.get("X-DyBot-Token")
                 or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                 or request.query.get("token")
                 or request.cookies.get(COOKIE_NAME)
                 or "")
        if given and secrets.compare_digest(given, self.token):
            resp = await handler(request)
            if request.query.get("token"):
                # 用 query 带的 token 换成 HttpOnly cookie，之后刷新不用再带
                resp.set_cookie(COOKIE_NAME, self.token, httponly=True,
                                samesite="Lax", max_age=7 * 24 * 3600)
            return resp
        if path.startswith("/api/"):
            return web.json_response(
                {"ok": False, "error": "unauthorized",
                 "msg": "token 无效或缺失（面板 token 与 WS token 是两套凭据）"},
                status=401)
        return web.Response(status=401, content_type="text/html",
                            text=_login_hint_page(self.title))

    # ------------------------------------------------------------ 页面
    async def page_index(self, request):
        return web.Response(content_type="text/html", text=_INDEX_HTML
                            .replace("__TITLE__", self.title))

    async def page_login(self, request):
        return web.Response(content_type="text/html", text=_LOGIN_HTML
                            .replace("__TITLE__", self.title))

    # ------------------------------------------------------------ 只读 API
    async def api_healthz(self, request):
        return web.json_response({"ok": True, "ts": time.time()})

    async def api_status(self, request):
        return web.json_response({"ok": True, "data": self._snapshot()})

    async def api_upstream(self, request):
        return web.json_response({"ok": True,
                                  "data": self._upstream_status()})

    async def api_logs(self, request):
        if self.hub is None:
            return web.json_response({"ok": True, "data": [],
                                      "stats": {"path": "", "total": 0}})
        q = request.query
        try:
            limit = int(q.get("limit") or 300)
        except ValueError:
            limit = 300
        since = q.get("since")
        try:
            since_seq = int(since) if since not in (None, "") else None
        except ValueError:
            since_seq = None
        rows = self.hub.tail(limit=limit, since_seq=since_seq,
                             level=q.get("level") or None)
        return web.json_response({"ok": True, "data": rows,
                                  "stats": self.hub.stats()})

    async def api_logs_stream(self, request):
        if self.hub is None:
            return web.json_response({"ok": False, "msg": "日志中枢未启用"},
                                     status=503)
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = self.hub.subscribe()
        try:
            # 先补最近若干行，避免刚打开是空的
            for rec in self.hub.tail(limit=120):
                await resp.write(_sse("log", rec))
            while True:
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")     # 心跳保活
                    continue
                await resp.write(_sse("log", rec))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as err:
            log(f"日志流中断: {type(err).__name__}: {err}", "WARN")
        finally:
            self.hub.unsubscribe(q)
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def api_config(self, request):
        return web.json_response({"ok": True, "data": self._config_view()})

    # ------------------------------------------------------------ 登录 API
    async def api_login_status(self, request):
        if self.login is None:
            return web.json_response({"ok": False, "msg": "登录模块未启用"},
                                     status=503)
        return web.json_response({"ok": True, "data": self.login.status()})

    async def api_login_qr(self, request):
        if self.login is None:
            return web.json_response({"ok": False, "msg": "登录模块未启用"},
                                     status=503)
        return web.json_response({"ok": True, "data": self.login.qr()})

    async def api_login_start(self, request):
        if self.login is None:
            return web.json_response({"ok": False, "msg": "登录模块未启用"},
                                     status=503)
        ok, msg = await asyncio.to_thread(self.login.start)
        return web.json_response({"ok": ok, "msg": msg},
                                 status=200 if ok else 409)

    async def api_login_cancel(self, request):
        if self.login is None:
            return web.json_response({"ok": False, "msg": "登录模块未启用"},
                                     status=503)
        ok, msg = await asyncio.to_thread(self.login.cancel)
        return web.json_response({"ok": ok, "msg": msg})

    # ------------------------------------------------------------ 上游 API
    async def api_upstream_reconnect(self, request):
        if self.supervisor is None:
            return web.json_response({"ok": False, "msg": "上游看护未启用"},
                                     status=503)
        await asyncio.to_thread(self.supervisor.force_reconnect, "面板手动重连")
        return web.json_response({"ok": True, "msg": "已触发重连",
                                  "data": self._upstream_status()})

    async def api_upstream_reload(self, request):
        if self.supervisor is None:
            return web.json_response({"ok": False, "msg": "上游看护未启用"},
                                     status=503)
        await asyncio.to_thread(self.supervisor.request_token_reload,
                                "面板手动换 token")
        return web.json_response({"ok": True, "msg": "已触发换 token 重连",
                                  "data": self._upstream_status()})

    # ------------------------------------------------------------ 数据组装
    def _upstream_status(self):
        if self.supervisor is None:
            return {"state": "disabled", "state_text": "未启用",
                    "connected": False}
        try:
            return self.supervisor.status()
        except Exception as err:
            return {"state": "error", "state_text": "状态读取失败",
                    "connected": False,
                    "last_error": f"{type(err).__name__}: {err}"}

    def _login_view(self):
        if self.login is None:
            return {"state": "disabled", "state_text": "未启用"}
        return self.login.status()

    def _onebot_view(self):
        bridge = self.bridge
        if bridge is None:
            return {"sinks": 0, "connections": []}
        conns = []
        for sink in list(getattr(bridge, "sinks", [])):
            conns.append({"name": getattr(sink, "name", "ws"),
                          "role": getattr(sink, "role", "universal"),
                          "alive": bool(getattr(sink, "alive", False))})
        return {"sinks": len(conns), "connections": conns,
                "self_id": getattr(bridge, "self_id", 0)}

    def _snapshot(self):
        bridge = self.bridge
        stats = {}
        for name in ("stat_messages_in", "stat_events_out", "stat_actions",
                     "stat_errors"):
            if bridge is not None and hasattr(bridge, name):
                stats[name] = getattr(bridge, name)
        return {
            "ts": time.time(),
            "uptime_sec": round(time.time() - self.started_at, 1),
            "self_uid": getattr(getattr(bridge, "im", None), "self_uid", ""),
            "upstream": self._upstream_status(),
            "login": self._login_view(),
            "onebot": self._onebot_view(),
            "stats": stats,
            "log": (self.hub.stats() if self.hub is not None else {}),
        }

    def _config_view(self):
        cfg = self.config
        fw = cfg.get("forward_ws") or {}
        rv = cfg.get("reverse_ws") or {}
        ha = cfg.get("http_api") or {}
        wu = cfg.get("webui") or {}
        creds, has_cookie = ({}, False)
        try:
            from .login import read_env_creds
            creds, has_cookie = read_env_creds()
        except Exception:
            pass
        return {
            "config_path": cfg.get("_config_path") or "",
            "self_id": cfg.get("self_id") or 0,
            "send_interval_sec": cfg.get("send_interval_sec"),
            "onebot": {
                "forward_ws": {"enabled": bool(fw.get("enabled")),
                               "host": fw.get("host"), "port": fw.get("port"),
                               "path": fw.get("path"),
                               "token_set": bool(fw.get("access_token"))},
                "reverse_ws": {"enabled": bool(rv.get("enabled")),
                               "urls": list(rv.get("urls") or []),
                               "token_set": bool(rv.get("access_token"))},
                "http_api": {"enabled": bool(ha.get("enabled")),
                             "host": ha.get("host"), "port": ha.get("port"),
                             "token_set": bool(ha.get("access_token"))},
            },
            "webui": {"enabled": True, "host": self.hosts, "port": self.port,
                      "token_set": bool(self.token),
                      "token_preview": _mask(self.token) if self.token else ""},
            "token_separate": True,     # 面板 token 与 ws token 是两套
            "login_creds": creds,
            "login_has_cookie": has_cookie,
            "douyin_ws": dict(cfg.get("douyin_ws") or {}),
        }


def _sse(event, payload):
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


# ---------------------------------------------------------------- 内嵌页面

_SHARED_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0b0d12;color:#e6e9ef;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 font-size:13.5px;line-height:1.6}
a{color:#60a5fa;text-decoration:none}
.wrap{max-width:1080px;margin:0 auto;padding:20px 18px 60px}
.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.top h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.3px}
.spacer{flex:1}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:#12161f;border:1px solid #1f2531;border-radius:14px;padding:16px}
.card h2{font-size:13px;margin:0 0 12px;color:#93a0b5;font-weight:600;letter-spacing:.6px;text-transform:uppercase}
.kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px dashed #1c222e}
.kv:last-child{border-bottom:0}
.kv b{font-weight:600;color:#e6e9ef}
.kv span{color:#8d99ad;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;text-align:right;word-break:break-all}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.ok{background:#0e2a1a;color:#4ade80}.ok .dot{background:#22c55e;box-shadow:0 0 8px #22c55e}
.warn{background:#2c2410;color:#fbbf24}.warn .dot{background:#f59e0b;box-shadow:0 0 8px #f59e0b}
.bad{background:#2c1417;color:#f87171}.bad .dot{background:#ef4444;box-shadow:0 0 8px #ef4444}
.idle{background:#1a1f2b;color:#94a3b8}.idle .dot{background:#64748b}
button{background:#1d2635;color:#e6e9ef;border:1px solid #2b3648;
 border-radius:9px;padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit}
button:hover{background:#26303f}
button:disabled{opacity:.45;cursor:not-allowed}
button.pri{background:#1d4ed8;border-color:#2563eb}
button.pri:hover{background:#2563eb}
button.danger{background:#7f1d1d;border-color:#991b1b}
.row{display:flex;gap:8px;flex-wrap:wrap}
#logbox{background:#0a0c10;border:1px solid #1f2531;border-radius:12px;padding:12px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.65;
 height:420px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.lv{font-weight:700}
.lv-INFO{color:#60a5fa}.lv-WARN{color:#fbbf24}.lv-ERROR,.lv-ERR,.lv-CRITICAL{color:#f87171}
.lv-OUT{color:#8d99ad}.lv-DEBUG,.lv-TRACE{color:#6b7280}
.ts{color:#5b6675}
.qrbox{background:#fff;border-radius:12px;padding:12px;display:inline-block}
.qrbox img{display:block;width:260px;height:260px;image-rendering:pixelated}
.muted{color:#8d99ad;font-size:12.5px}
.err{color:#f87171;font-size:12.5px}
"""

_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>__CSS__</style></head>
<body><div class="wrap">
<div class="top">
  <h1>__TITLE__</h1>
  <span id="upBadge" class="badge idle"><i class="dot"></i><span>读取中…</span></span>
  <span class="spacer"></span>
  <a href="/login">扫码登录</a>
  <button onclick="reconnect()">重连上游</button>
  <button onclick="reloadToken()">换 token 重连</button>
</div>

<div class="grid">
  <div class="card">
    <h2>抖音推送 WS（上游）</h2>
    <div id="upBody"><div class="muted">读取中…</div></div>
  </div>
  <div class="card">
    <h2>OneBot 连接</h2>
    <div id="obBody"><div class="muted">读取中…</div></div>
  </div>
  <div class="card">
    <h2>登录态</h2>
    <div id="lgBody"><div class="muted">读取中…</div></div>
  </div>
</div>

<div class="card" style="margin-top:14px">
  <h2>实时日志（可筛选）</h2>
  <div class="row" style="margin-bottom:10px">
    <button onclick="setLv('')">全部</button>
    <button onclick="setLv('INFO')">INFO+</button>
    <button onclick="setLv('WARN')">WARN+</button>
    <button onclick="setLv('ERROR')">ERROR</button>
    <span class="spacer"></span>
    <label class="muted"><input type="checkbox" id="autoScroll" checked> 自动滚动</label>
    <button onclick="clearLog()">清屏</button>
  </div>
  <div id="logbox"></div>
  <div class="muted" id="logStat" style="margin-top:8px"></div>
</div>
</div>
<script>
__JS__
</script></body></html>
"""

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码登录 · __TITLE__</title><style>__CSS__</style></head>
<body><div class="wrap">
<div class="top">
  <h1>抖音扫码登录</h1>
  <span class="spacer"></span>
  <a href="/">← 返回面板</a>
</div>
<div class="grid">
  <div class="card" style="text-align:center">
    <div id="qrWrap"><div class="muted">点击「开始扫码」生成二维码</div></div>
    <div id="qrHint" class="muted" style="margin-top:12px"></div>
  </div>
  <div class="card">
    <h2>状态</h2>
    <div id="lgBody"><div class="muted">读取中…</div></div>
    <div class="row" style="margin-top:14px">
      <button class="pri" onclick="startLogin()">开始扫码</button>
      <button class="danger" onclick="cancelLogin()">取消</button>
    </div>
    <div class="err" id="errBox" style="margin-top:10px"></div>
    <div class="muted" style="margin-top:14px">
      打开抖音 App →「我」→ 右上角 ☰ → 扫一扫。二维码约 60 秒自动换新。<br>
      登录成功后凭据写入 .env，上游 WS 会自动重连。
    </div>
  </div>
</div>
</div>
<script>
__JS__
</script></body></html>
"""

_COMMON_JS = """
async function api(path, opts){
  const r = await fetch(path, Object.assign({credentials:'same-origin'}, opts||{}));
  if(r.status===401){ location.href='/login?token='; throw new Error('unauthorized'); }
  return await r.json();
}
function kv(k,v){ return '<div class="kv"><b>'+esc(k)+'</b><span>'+esc(v)+'</span></div>'; }
function esc(s){
  return String(s===undefined||s===null?'':s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}
function fmtTs(t){
  if(!t) return '-';
  const d=new Date(t*1000);
  return d.toLocaleString('zh-CN',{hour12:false});
}
function badge(cls,text){ return '<span class="badge '+cls+'"><i class="dot"></i>'+esc(text)+'</span>'; }
function stateCls(state){
  if(state==='connected') return 'ok';
  if(state==='needs_login'||state==='error'||state==='failed') return 'bad';
  if(state==='reconnecting'||state==='connecting'||state==='waiting'
     ||state==='scanned'||state==='starting') return 'warn';
  return 'idle';
}
function upStateCls(s){
  if(s.connected) return 'ok';
  if(s.state==='needs_login'||s.state==='error'||s.state==='disabled') return 'bad';
  if(s.state==='stopped') return 'idle';
  return 'warn';
}
function renderUpstream(s){
  const b=document.getElementById('upBadge');
  if(b){ b.className='badge '+upStateCls(s); b.innerHTML='<i class="dot"></i>'+esc(s.state_text||s.state); }
  const el=document.getElementById('upBody');
  if(!el) return;
  el.innerHTML=
    kv('状态', s.state_text||s.state)+
    kv('重试级别', s.level_name||'-')+
    kv('连续失败', (s.failures!==undefined?s.failures:'-'))+
    kv('已存活', s.uptime_sec?(s.uptime_sec+' 秒'):'-')+
    kv('下次重连', s.next_retry_in?(s.next_retry_in+' 秒后'):'-')+
    kv('最近连接', fmtTs(s.last_open_at))+
    kv('最近断开', fmtTs(s.last_close_at))+
    kv('连接/断开', (s.connects||0)+' / '+(s.disconnects||0))+
    kv('自动重连次数', s.reconnects||0)+
    kv('换token次数', s.token_reloads||0)+
    kv('心跳', (s.ping_interval_sec||0)+' 秒')+
    kv('上游地址', s.ws_host||'-')+
    (s.last_error?('<div class="err" style="margin-top:8px">最近错误：'+esc(s.last_error)+'</div>'):'');
}
function renderOnebot(o){
  const el=document.getElementById('obBody');
  if(!el) return;
  let h=kv('self_id', o.self_id||'-')+kv('在线连接', o.sinks||0);
  (o.connections||[]).forEach(function(c,i){
    h+=kv('连接 '+(i+1), c.name+' · '+c.role+' · '+(c.alive?'在线':'已断'));
  });
  if(!(o.connections||[]).length) h+='<div class="muted" style="margin-top:8px">暂无 OneBot 客户端接入</div>';
  el.innerHTML=h;
}
function renderLogin(l){
  const el=document.getElementById('lgBody');
  if(!el) return;
  let h=kv('状态', l.state_text||l.state);
  if(l.detail) h+=kv('说明', l.detail);
  if(l.uid) h+=kv('uid', l.uid);
  if(l.creds && Object.keys(l.creds).length){
    Object.keys(l.creds).forEach(function(k){ h+=kv(k, l.creds[k]?'✔':'✘'); });
  }
  el.innerHTML=h;
  const cls=stateCls(l.state);
  if(el.parentElement && el.parentElement.querySelector('.badge')===null && l.state){
    // 登录卡片顶部小徽标（可选）
  }
}
"""

_INDEX_JS = _COMMON_JS + """
let logSince=0, logLevel='', logLines=0;
let es=null;

function setLv(v){ logLevel=v; reloadLogs(); }
function clearLog(){ document.getElementById('logbox').innerHTML=''; logLines=0; updateStat(); }
function updateStat(){
  document.getElementById('logStat').textContent='已显示 '+logLines+' 行 · 筛选 '+(logLevel||'全部');
}
function logLine(r){
  const box=document.getElementById('logbox');
  const div=document.createElement('div');
  div.innerHTML='<span class="ts">'+esc(r.time)+'</span> <span class="lv lv-'+esc(r.level)+'">'
    +esc(r.level)+'</span> '+esc(r.msg);
  box.appendChild(div);
  while(box.childNodes.length>2000) box.removeChild(box.firstChild);
  logLines++;
  if(document.getElementById('autoScroll').checked) box.scrollTop=box.scrollHeight;
  updateStat();
}
function keep(r){
  if(!logLevel) return true;
  const order={TRACE:5,DEBUG:10,OUT:15,INFO:20,WARN:30,ERR:35,ERROR:40,CRITICAL:50};
  return (order[r.level]||20) >= (order[logLevel]||0);
}
async function reloadLogs(){
  const j=await api('/api/logs?limit=500'+(logLevel?('&level='+logLevel):''));
  const box=document.getElementById('logbox');
  box.innerHTML=''; logLines=0;
  (j.data||[]).forEach(function(r){ if(keep(r)){ logLine(r); logSince=Math.max(logSince,r.seq); } });
}
function startStream(){
  if(es) es.close();
  es=new EventSource('/api/logs/stream');
  es.addEventListener('log', function(e){
    const r=JSON.parse(e.data);
    if(r.seq<=logSince) return;
    logSince=r.seq;
    if(keep(r)) logLine(r);
  });
  es.onerror=function(){ /* 浏览器自动重连 */ };
}
async function refresh(){
  try{
    const j=await api('/api/status');
    const d=j.data||{};
    renderUpstream(d.upstream||{});
    renderOnebot(d.onebot||{});
    renderLogin(d.login||{});
  }catch(e){}
}
async function reconnect(){
  const j=await api('/api/upstream/reconnect',{method:'POST'});
  if(j.data) renderUpstream(j.data);
}
async function reloadToken(){
  const j=await api('/api/upstream/reload-token',{method:'POST'});
  if(j.data) renderUpstream(j.data);
}
(async function(){
  await refresh(); await reloadLogs(); startStream();
  setInterval(refresh, 2000);
  setInterval(async function(){
    if(es && es.readyState===2) startStream();
  }, 10000);
})();
"""

_LOGIN_JS = _COMMON_JS + """
let lastQrCount=-1;

async function refreshLogin(){
  try{
    const j=await api('/api/login/status');
    const l=j.data||{};
    renderLogin(l);
    const hint=document.getElementById('qrHint');
    if(l.state==='waiting'||l.state==='scanned'){
      hint.textContent='第 '+(l.qr_count||1)+' 张二维码 · 已等待 '+Math.round(l.elapsed_sec||0)+' 秒';
      if((l.qr_count||0)!==lastQrCount){ lastQrCount=l.qr_count||0; await loadQr(); }
    } else if(l.state==='success'){
      hint.textContent='登录成功'+(l.uid?('（uid '+l.uid+'）'):'');
      document.getElementById('qrWrap').innerHTML='<div class="badge ok"><i class="dot"></i>登录成功</div>';
    } else if(l.state==='failed'){
      hint.textContent='';
      document.getElementById('errBox').textContent=l.detail||'登录失败';
    } else if(l.state==='idle'){
      hint.textContent='';
    }
  }catch(e){}
}
async function loadQr(){
  const j=await api('/api/login/qr');
  const q=(j.data||{}).qr;
  if(q){ document.getElementById('qrWrap').innerHTML=
    '<div class="qrbox"><img src="'+q+'" alt="二维码"></div>'; }
}
async function startLogin(){
  document.getElementById('errBox').textContent='';
  const j=await api('/api/login/start',{method:'POST'});
  if(!j.ok) document.getElementById('errBox').textContent=j.msg||'启动失败';
  await refreshLogin();
}
async function cancelLogin(){
  const j=await api('/api/login/cancel',{method:'POST'});
  if(!j.ok) document.getElementById('errBox').textContent=j.msg||'取消失败';
  await refreshLogin();
}
(async function(){ await refreshLogin(); setInterval(refreshLogin,1500); })();
"""

_INDEX_HTML = (_INDEX_HTML
               .replace("__CSS__", _SHARED_CSS)
               .replace("__JS__", _INDEX_JS))
_LOGIN_HTML = (_LOGIN_HTML
               .replace("__CSS__", _SHARED_CSS)
               .replace("__JS__", _LOGIN_JS))


def _login_hint_page(title):
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><style>{_SHARED_CSS}</style></head><body><div class="wrap">
<div class="card" style="max-width:520px;margin:12vh auto">
<h2>需要 token</h2>
<p class="muted">面板启用了访问 token（<code>DYBOT_WEBUI_TOKEN</code>）。
面板 token 与 OneBot 的 WS token 是两套独立凭据，请不要混用。</p>
<p class="muted">请在地址后附加 <code>?token=你的面板token</code> 访问一次，
之后浏览器会记住。</p>
</div></div></body></html>"""
