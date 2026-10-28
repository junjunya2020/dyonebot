# -*- coding: utf-8 -*-
"""dyonebot 桥的启动入口。

    cd /root/dyonebot/douyin-spider
    .venv/bin/python -u /root/dyonebot/dy-bot/run_bridge.py

只做三件事：读配置 -> 建抖音 IM 门面（含登录态检查）-> 跑 Bridge。
所有协议细节都在 onebot_bridge/ 里，这里不含业务逻辑。
"""
import asyncio
import os
import signal
import sys
import time

sys.path.insert(0, "/root/dyonebot/dy-bot")
sys.path.insert(0, "/root/dyonebot/douyin-spider")

from onebot_bridge import config as config_mod  # noqa: E402
from onebot_bridge import logutil  # noqa: E402
from onebot_bridge.bridge import Bridge, build_self_id, log  # noqa: E402
from onebot_bridge.dyim import DouyinIM  # noqa: E402
from onebot_bridge.ids import IdMap  # noqa: E402

AUTH_ENV_FILE = "/root/dyonebot/douyin-spider/.env"


def check_credentials():
    """读 .env 判断登录态。

    不能靠 load_env() 返回值判断：DY_COOKIES 为空时它会自己开一个扫码登录流
    并阻塞，等于桥启动时又抢一次二维码。
    """
    env_file = AUTH_ENV_FILE
    if not os.path.exists(env_file):
        return False, f"{env_file} 不存在"
    for line in open(env_file, encoding="utf-8"):
        if line.startswith("DY_COOKIES="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            return (bool(value), "" if value else "DY_COOKIES 为空")
    return False, "DY_COOKIES 未设置"


async def main():
    cfg = config_mod.load()
    config_mod.write_default(cfg["_config_path"])

    # 日志中枢必须在这里装：面板的「日志查看」直接读它。
    # 之前漏了这一步 -> HUB 永远是 None -> 面板日志页恒为空（total=0），
    # 而上游掉线原因恰恰只能从这些行里看出来。
    logutil.setup(cfg.get("log_file"),
                  capacity=int(cfg.get("log_ring_capacity", 3000) or 3000),
                  max_bytes=int(cfg.get("log_max_bytes", 20 * 1024 * 1024) or 0))
    log(f"配置 {cfg['_config_path']}")

    has_cred, why = check_credentials()
    if not has_cred:
        log(f"ABORT 未登录：{why}", "ERROR")
        log("      先跑 /root/dyonebot/dy-bot/start_login.sh 扫码登录", "ERROR")
        return 1

    # Pass the absolute credential path through DouyinIM as well.  Its loader
    # must override inherited DY_* values, otherwise a restart from a service
    # environment can accidentally mask the persisted login.
    im = DouyinIM()
    log(f"抖音登录态 OK self_uid={im.self_uid}")

    self_id = build_self_id(cfg.get("self_id"), im.self_uid)
    idmap = IdMap(cfg["state_file"])
    # 登记自己的 uid <-> self_id 双射：@机器人 还原成 OneBot id、
    # 回 @ 时把 self_id 换回抖音 uid，都依赖它。
    idmap.register_self(im.self_uid, self_id)
    log(f"OneBot self_id={self_id}（抖音 uid {im.self_uid}）")

    # 抖音图片是 AES-256-GCM 密文，必须由本桥解密后再回给 OneBot 客户端。
    # 建在这里（而不是 Bridge.run 里），是因为事件构造时就要用到它登记 token。
    from onebot_bridge.media import MediaCache
    media = MediaCache(cfg.get("media") or {})
    if media.enabled:
        log(f"图片解密服务已启用：对外 {media.public_base}{media.path}/<token>，"
            f"缓存 {media.dir}（TTL {int(media.ttl)}s）")

    bridge = Bridge(cfg, im, idmap, self_id, media=media)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bridge.stop)
        except NotImplementedError:
            pass

    log("桥已启动，Ctrl-C 停止")
    try:
        await bridge.run()
    finally:
        idmap.save()
        log("桥已停止")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
