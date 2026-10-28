# -*- coding: utf-8 -*-
"""CDP 侦察：把抖音 PC 网页 IM 的 BytedIM SDK 内部端点表挖出来。

目的：找出 撤回消息 / 群成员列表 / 群列表 的真实 endpoint 与协议字段。

做法：
  1. 用真 Chrome 以 CDP 打开 https://www.douyin.com/chat（复用 .env 里的 cookie）
  2. 等 SDK 初始化完成（window 上有 bytedIM 实例）
  3. 在页面里遍历 SDK 的方法表 / 网络层常量，导出所有 endpoint 字符串
  4. 同时包装 fetch / XHR，把真实请求记录下来

输出：/root/dyonebot/dy-bot/recon_out.json
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/root/dyonebot/douyin-spider")
os.chdir("/root/dyonebot/douyin-spider")

CHROME = "/root/其他/live2d-own-model/.pw4/chromium-1243/chrome-linux64/chrome"
PORT = 9223
PROFILE = "/tmp/dy_recon_profile"
OUT = Path("/root/dyonebot/dy-bot/recon_out.json")


def load_cookies():
    from utils.common_util import load_env
    auth = load_env()
    return auth.cookie, auth


def start_chrome():
    subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    subprocess.run(["rm", "-rf", PROFILE], capture_output=True)
    time.sleep(0.5)
    proc = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--remote-allow-origins=http://127.0.0.1:{PORT}",
        f"--user-data-dir={PROFILE}", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        "--window-size=1400,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
                data = json.load(r)
                print("chrome up:", data.get("Browser"), flush=True)
                return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome 没起来")


class CDP:
    def __init__(self, ws_url):
        from websocket import create_connection
        self.ws = create_connection(ws_url, timeout=60)
        self.id = 0

    def call(self, method, params=None, timeout=60):
        self.id += 1
        mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

    def eval(self, expr, timeout=60, await_promise=False):
        res = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": True,
        }, timeout=timeout)
        return res.get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


PROBE_JS = r"""
(() => {
  const out = {found: [], endpoints: [], version: null, notes: []};

  // ---- 1. 找 SDK 实例 ------------------------------------------------
  const cands = [
    ['window.bytedIM', () => window.bytedIM],
    ['window.__bytedIM', () => window.__bytedIM],
    ['window.ImSdk', () => window.ImSdk],
    ['window.__IM_SDK__', () => window.__IM_SDK__],
  ];
  for (const [name, fn] of cands) {
    try { const v = fn(); if (v) out.found.push(name); } catch (e) {}
  }

  // 遍历 window 找带 bytedIMInstance 的对象
  const imHosts = [];
  for (const k of Object.keys(window)) {
    try {
      const v = window[k];
      if (v && typeof v === 'object' && v.bytedIMInstance) {
        imHosts.push(k);
        if (!out.found.includes('window.' + k)) out.found.push('window.' + k);
      }
    } catch (e) {}
  }
  out.imHosts = imHosts;

  // ---- 2. 从实例上抓方法名 ------------------------------------------
  const inst = (() => {
    for (const k of imHosts) {
      try { const i = window[k].bytedIMInstance; if (i) {
        const p = i.promise ? 'deferred' : 'direct'; return {k, i, p};
      } } catch (e) {}
    }
    return null;
  })();

  const methodBag = {};
  if (inst) {
    let proto = inst.i;
    let depth = 0;
    while (proto && depth < 5) {
      for (const name of Object.getOwnPropertyNames(proto)) {
        if (typeof name !== 'string') continue;
        methodBag[name] = true;
      }
      proto = Object.getPrototypeOf(proto);
      depth++;
    }
  }
  out.sdkMethods = Object.keys(methodBag).sort();

  // ---- 3. 全局搜索 endpoint 字符串 ----------------------------------
  // SDK 常量表可能挂在闭包里，改从已有的模块注册表反查
  const epRe = /^\/v[12]\/[a-zA-Z0-9_\/]+$/;
  const seen = new Set();
  function walk(obj, depth, path) {
    if (depth > 4 || obj === null || obj === undefined) return;
    const t = typeof obj;
    if (t === 'string') {
      if ((obj.startsWith('/v1/') || obj.startsWith('/v2/')) && obj.length < 80) {
        if (!seen.has(obj)) { seen.add(obj); out.endpoints.push({ep: obj, at: path}); }
      }
      return;
    }
    if (t !== 'object') return;
    let keys;
    try { keys = Object.getOwnPropertyNames(obj); } catch (e) { return; }
    for (const k of keys) {
      if (k === 'window' || k === 'document' || k === 'location') continue;
      let v;
      try { v = obj[k]; } catch (e) { continue; }
      if (v === obj) continue;
      walk(v, depth + 1, path + '.' + k);
    }
  }
  try { walk(window, 0, 'window'); } catch (e) { out.notes.push('walk: ' + e.message); }

  return out;
})()
"""

RECORD_JS = r"""
(() => {
  if (window.__recInstalled) return 'already';
  window.__recInstalled = true;

  window.__reqLog = [];
  window.__pushReq = (entry) => {
    try {
      if (window.__reqLog.length < 500) window.__reqLog.push(entry);
    } catch (e) {}
  };

  const of = window.fetch;
  window.fetch = function(input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (/imapi|douyin\.com\/(aweme\/v1\/web\/im|v1\/web\/im)/.test(url)) {
        let body = null;
        try {
          if (init && typeof init.body === 'string') body = init.body.slice(0, 4000);
          else if (init && init.body && init.body.byteLength) {
            body = 'BYTES len=' + init.body.byteLength;
          }
        } catch (e) {}
        window.__pushReq({kind: 'fetch', url, method: (init && init.method) || 'GET',
                          body, t: Date.now()});
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };

  const oOpen = XMLHttpRequest.prototype.open;
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) {
    this.__m = m; this.__u = u;
    return oOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(b) {
    try {
      const u = this.__u || '';
      if (/imapi|douyin\.com\/(aweme\/v1\/web\/im|v1\/web\/im)/.test(u)) {
        let body = null;
        try {
          if (typeof b === 'string') body = b.slice(0, 4000);
          else if (b && b.byteLength) body = 'BYTES len=' + b.byteLength;
        } catch (e) {}
        window.__pushReq({kind: 'xhr', url: u, method: this.__m || 'GET', body, t: Date.now()});
      }
    } catch (e) {}
    return oSend.apply(this, arguments);
  };
  return 'installed';
})()
"""


def main():
    auth_cookie, auth = load_cookies()
    print("my_uid:", auth.get_uid(), "cookies:", len(auth_cookie), flush=True)

    proc = start_chrome()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
            targets = json.load(r)
        page = [t for t in targets if t["type"] == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Network.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        # 注入 cookie
        ok = 0
        for name, value in auth_cookie.items():
            r = cdp.call("Network.setCookie", {
                "name": name, "value": str(value), "domain": ".douyin.com",
                "path": "/", "secure": True,
            })
            if r.get("result", {}).get("success"):
                ok += 1
        print(f"injected cookies: {ok}/{len(auth_cookie)}", flush=True)

        cdp.call("Page.navigate", {"url": "https://www.douyin.com/chat"})
        time.sleep(6)

        print("install recorder:", cdp.eval(RECORD_JS), flush=True)
        # 给 SDK 时间初始化 & 拉会话
        time.sleep(14)

        probe = cdp.eval(PROBE_JS, timeout=120)
        reqs = cdp.eval("JSON.stringify(window.__reqLog || [])", timeout=60)
        try:
            reqs = json.loads(reqs) if reqs else []
        except Exception:
            reqs = []

        result = {"probe": probe, "requests": reqs}
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        print("=== found:", probe.get("found"), flush=True)
        print("=== sdkMethods:", len(probe.get("sdkMethods") or []), flush=True)
        for m in (probe.get("sdkMethods") or []):
            if re.search(r"(?i)recall|revoke|participant|member|group|conversation|delete|list", m):
                print("   ", m, flush=True)
        print("=== endpoints:", len(probe.get("endpoints") or []), flush=True)
        for e in (probe.get("endpoints") or [])[:60]:
            print("   ", e["ep"], flush=True)
        print("=== requests:", len(reqs), flush=True)
        for r_ in reqs[:40]:
            print("   ", r_["kind"], r_["method"], r_["url"][:120], flush=True)
        print("saved ->", OUT, flush=True)
        cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
