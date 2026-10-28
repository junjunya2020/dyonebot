# -*- coding: utf-8 -*-
"""第 2 步：列出 /chat 页面实际加载的所有 JS，下载后离线 grep 端点表。

SDK 的端点常量埋在 webpack 闭包里，但从网络层能拿到 bundle 的真实 URL。
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
PORT = 9224
PROFILE = "/tmp/dy_recon2_profile"
SCRIPT_DIR = Path("/tmp/dyjs/live_scripts")
OUT = Path("/root/dyonebot/dy-bot/recon_scripts.json")

sys.path.insert(0, "/root/dyonebot/dy-bot")
from recon_im_sdk import CDP, load_cookies, start_chrome  # noqa

# 覆盖端口/配置
import recon_im_sdk as R
R.PORT = PORT
R.PROFILE = PROFILE


LIST_JS = r"""
JSON.stringify((() => {
  const out = {resources: [], scripts: [], nav: []};
  try {
    for (const e of performance.getEntriesByType('resource')) {
      if (e.initiatorType === 'script' || /\.js(\?|$)/.test(e.name)) {
        out.resources.push({url: e.name, size: e.transferSize || 0});
      }
    }
  } catch (err) {}
  try {
    for (const s of document.querySelectorAll('script[src]')) out.scripts.push(s.src);
  } catch (err) {}
  return out;
})())
"""


def main():
    auth_cookie, auth = load_cookies()
    print("my_uid:", auth.get_uid(), flush=True)

    proc = start_chrome()
    cdp = None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
            targets = json.load(r)
        page = [t for t in targets if t["type"] == "page"][0]
        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Network.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        for name, value in auth_cookie.items():
            cdp.call("Network.setCookie", {
                "name": name, "value": str(value), "domain": ".douyin.com",
                "path": "/", "secure": True,
            })
        cdp.call("Page.navigate", {"url": "https://www.douyin.com/chat"})
        time.sleep(18)

        # 打开一个会话，让 IM SDK 真正把消息/会话逻辑跑起来
        got = cdp.eval(LIST_JS, timeout=60)
        data = json.loads(got) if got else {}
        urls = sorted({x["url"] for x in data.get("resources", [])} |
                      set(data.get("scripts", [])))
        print("loaded scripts:", len(urls), flush=True)

        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, u in enumerate(urls):
            if not u.startswith("http"):
                continue
            fn = SCRIPT_DIR / f"{i:03d}_{re.sub(r'[^A-Za-z0-9._-]', '_', u.split('/')[-1])}"
            try:
                subprocess.run(["curl", "-s", "-m", "45", u, "-o", str(fn)], check=False)
                size = fn.stat().st_size if fn.exists() else 0
                manifest.append({"url": u, "file": str(fn), "size": size})
            except Exception as e:
                manifest.append({"url": u, "file": None, "error": str(e)})
        OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("downloaded:", sum(1 for m in manifest if m.get("size")), flush=True)
        print("saved ->", OUT, flush=True)
    finally:
        if cdp:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
