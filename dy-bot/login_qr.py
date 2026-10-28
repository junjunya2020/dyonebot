# -*- coding: utf-8 -*-
"""扫码登录：把二维码渲染成 PNG，成功后把六件套凭据写入项目 .env。

放在仓库外，不改动任何项目源码。
"""
import os
import sys
import time
from pathlib import Path

import qrcode

from dy_apis.login_api import DYLoginApi

OUT = Path("/root/dyonebot/dy-bot")
QR_PATH = OUT / "login_qr.png"
LOG = OUT / "login_log.txt"

_gen = {"n": 0}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def on_qr(url):
    _gen["n"] += 1
    img = qrcode.make(url)
    img.save(QR_PATH)
    log(f"QR#{_gen['n']} written (expires ~60s) url={url[:70]}")


def main():
    if LOG.exists():
        LOG.unlink()
    # 常驻等待：二维码每秒过一张，一直刷到用户扫为止。
    timeout = int(os.environ.get("DYBOT_QR_TIMEOUT", "7200"))
    log(f"login start (qr wait timeout={timeout}s)")
    api = DYLoginApi()
    try:
        auth = api.qrcode_login(timeout=timeout, show_qr=False, on_qrcode=on_qr)
    except Exception as err:
        log(f"LOGIN_FAILED {type(err).__name__}: {err}")
        return 1

    path = api.save_credential(auth)
    log(f"credential saved -> {path}")
    log(f"sessionid   : {bool(auth.cookie.get('sessionid'))}")
    log(f"ticket      : {bool(auth.ticket)}")
    log(f"ts_sign     : {bool(auth.ts_sign)}")
    log(f"client_cert : {bool(auth.client_cert)}")
    log(f"private_key : {bool(auth.private_key)}")
    log(f"dtrait_blob : {bool(auth.dtrait_blob)}")
    log(f"cookie_count: {len(auth.cookie)}")
    try:
        log(f"my_uid      : {auth.get_uid()}")
    except Exception as err:
        log(f"my_uid error: {err}")
    rep = auth.capability_report()
    log(f"read_only_ready        : {rep.get('read_only_ready')}")
    log(f"publish_security_ready : {rep.get('publish_security_ready')}")
    log(f"publish_security_missing: {rep.get('publish_security_missing')}")
    log("LOGIN_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
