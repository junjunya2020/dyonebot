# -*- coding: utf-8 -*-
"""配置加载。

优先级：环境变量 > 配置文件 > 内置默认值。
配置文件是 JSON（机器可写），不给用户手改注释的负担；
所有键都能用 DYBOT_OB_<大写键名> 环境变量覆盖。
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    # ---------- 通用 ----------
    "log_file": "/root/dyonebot/dy-bot/onebot_bridge.log",
    "state_file": "/root/dyonebot/dy-bot/onebot_bridge_state.json",
    "log_level": "INFO",

    # 桥在 OneBot 侧暴露的「机器人账号」。
    # 抖音没有 QQ 号，这里用登录 uid 映射一个高位 int64：0 表示自动
    # （7000000000000000000 + uid % 10^15，实测抖音 uid 3.6e15 量级，
    #   结果落在 7.0e18 附近，int64 上限 9.22e18 之内，且远离群号 7.7e18）。
    "self_id": 0,

    # 抖音会把「自己在手机/网页端发的消息」也推回来。默认不再回灌给 OneBot，
    # 否则机器人会对自己的消息再回复一次（死循环）。
    "skip_self": True,

    # 群系统通知（message_type 1001 / aweType 903 这类「XX 加入了群聊」）默认不推给
    # OneBot：它们不是用户发言，推过去容易被大模型当成一句待回复的话。
    "emit_notices": False,

    # ---------- 发送限速 ----------
    # 抖音风控对同一会话的连续发送比较敏感，默认给一个温和的间隔。
    "send_interval_sec": 0.8,

    # ---------- 抖音推送 WS（上游，桥主动连抖音）----------
    # 上游自带的 on_error 只认 ConnectionRefusedError /
    # WebSocketConnectionClosedException，而心跳超时抛的是
    # WebSocketTimeoutException（两者是兄弟类，不是子类），所以上游的
    # 自动重连在「心跳超时」这条最常见的路径上是失效的。桥自己看护重连，
    # 分三级：直连重连 -> 换 token 重连 -> 提示重新登录。
    "douyin_ws": {
        # WebSocketApp.run_forever 的 ping_interval 默认 0（不发心跳），
        # 实测不发心跳的连接约 30~60 秒被服务端回收，所以必须发。
        "ping_interval_sec": 25,
        "reconnect_min_sec": 2.0,     # 退避起点
        "reconnect_max_sec": 60.0,    # 退避上限
        "direct_retries": 5,          # 1 级：同一凭据直连重试次数
        "token_retries": 3,           # 2 级：换 token 重试次数
        "stable_sec": 60.0,           # 存活超过这个秒数就算「稳定」，级别回落
        # 网页端每 300 秒发送一次 mssdk 行为心跳并轮换 msToken；WS ping
        # 只能维持 TCP，不能替代这条会话保活。
        "mssdk_heartbeat_sec": 300.0,
    },

    # ---------- 事件泵：撤回补齐 ----------
    # 抖音的撤回只有一个 mt=40001 的事件，推送里【不带】被撤回消息的原文，
    # 也不一定能稳定推到全部端。所以定期用 301 历史接口把 s:is_recalled 标出来，
    # 对账后补发 OneBot 的 notice/group_recall、notice/friend_recall。
    "recall_poll": {
        "enabled": True,
        "interval_sec": 20,
        "history_limit": 30,
        "scope": "groups",      # groups | all | none
    },

    # ---------- 群成员昵称预取 ----------
    # OneBot 的 sender.nickname / get_group_member_info 都要昵称，但抖音推送里
    # 只有 sec_uid。这里在启动后（以及在群里见到新成员时）后台线程池预取并缓存，
    # 保证事件构造时【绝不阻塞】。
    "profile_prefetch": {
        "enabled": True,
        "on_start": True,
        "max_members": 200,
        "group_limit": 20,
    },

    # ---------- 反向 WS（桥 -> AstrBot） ----------
    "reverse_ws": {
        "enabled": True,
        "urls": ["ws://127.0.0.1:6199/ws"],
        "access_token": "",     # 与 AstrBot 的 ws_reverse_token 一致；留空则不带
        "reconnect_min_sec": 1.0,
        "reconnect_max_sec": 30.0,
    },

    # ---------- 正向 WS（AstrBot -> 桥） ----------
    "forward_ws": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 6700,
        "path": "/",
        "access_token": "",
        "heartbeat_sec": 30,    # OneBot 心跳事件间隔，0 关闭
    },

    # ---------- 正向 HTTP API ----------
    # OneBot v11 的 HTTP 通信方式：AstrBot 用 POST /<action> 调桥。
    "http_api": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 6701,
        "path": "/",
        "access_token": "",
    },

    # ---------- HTTP POST 事件上报 ----------
    # 桥把事件 POST 到配置的 URL（OneBot v11 的 HTTP 上报方式）。
    "http_post": {
        "enabled": False,
        "urls": [],
        "secret": "",
        "timeout_sec": 8,
    },

    # ---------- 控制面板 ----------
    # 面板 token 与 OneBot 的 WS token 是【两套独立凭据】：
    #   webui.token       只进浏览器（面板登录 / 查状态 / 看日志 / 扫码）
    #   forward_ws.access_token / reverse_ws.access_token / http_api.access_token
    #                     OneBot 客户端用（AstrBot / NapCat）
    # 任何一边泄露都不会让对方失守；面板里也绝不回显 WS token 明文。
    #
    # 环境变量：
    #   DYBOT_WEBUI_TOKEN  面板 token
    #   DYBOT_WS_TOKEN     OneBot token 便捷键，一次性写进上面三个位置
    # 两者都留空时面板只监听 127.0.0.1 且不校验（本机自用）。
    "webui": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 6800,
        "token": "",
        "title": "dyonebot 控制面板",
    },

    # ---------- 加密图片的解密/代理服务 ----------
    # 抖音图片消息（type 27）给的是 AES-256-GCM 密文（key = resource_url.skey，
    # 12 字节 nonce 前缀），CDN 直链直接丢给 OneBot 客户端是显示不出来的。
    # 桥把直链换成自己的短 URL，客户端来取时才「下载 -> 解密 -> 回图」。
    # public_host 留空则自动探测（docker0 网关优先，AstrBot 容器可直达）。
    "media": {
        "enabled": True,
        "dir": "/root/dyonebot/dy-bot/media_cache",
        "path": "/media",
        "port": 6801,
        "public_host": "",          # 留空 -> 自动探测
        "public_base": "",          # 留空 -> http://{public_host}:{port}
        "ttl_sec": 3600,            # 明文图片保留时长
        "max_files": 400,           # 明文图片数量上限
        "fetch_timeout_sec": 20,
    },

    # ---------- 日志中枢 ----------
    # 环形缓冲容量 + 单文件轮转上限（WebUI 日志查看的数据源）。
    "log_ring_capacity": 3000,
    "log_max_bytes": 20971520,
}

# DYBOT_WS_TOKEN 这一个环境变量要同时写进下面三个 OneBot token 位置：
# 用户只需要记一个名字，而桥内部仍按 OneBot 各通信方式各自的字段读。
_WS_TOKEN_TARGETS = (("forward_ws", "access_token"),
                     ("reverse_ws", "access_token"),
                     ("http_api", "access_token"))

_ENV_PREFIX = "DYBOT_OB_"


def _coerce(raw, default):
    """按默认值的类型把字符串转回来。"""
    if isinstance(default, bool):
        return str(raw).strip().lower() not in ("", "0", "false", "no", "off")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    return raw


def _apply_env(cfg, prefix=_ENV_PREFIX, node=None):
    """把 DYBOT_OB_A_B 形式的变量写回 cfg['a']['b']。"""
    if node is None:
        node = cfg
    for key, value in list(node.items()):
        env_key = prefix + key.upper()
        if isinstance(value, dict):
            _apply_env(cfg, prefix=env_key + "_", node=value)
            continue
        if env_key in os.environ:
            node[key] = _coerce(os.environ[env_key], value)


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_special_env(cfg):
    """处理两个「便捷」环境变量（不带 DYBOT_OB_ 前缀，用户更好记）。

    DYBOT_WEBUI_TOKEN  面板 token（= webui.token）
    DYBOT_WS_TOKEN     一次性写进 forward_ws / reverse_ws / http_api 的
                       access_token —— 用户只记一个名字，内部仍旧按各通信
                       方式各自读，这样两种配法在代码里是同一份语义。
    """
    webui_token = os.environ.get("DYBOT_WEBUI_TOKEN")
    if webui_token is not None:
        node = cfg.setdefault("webui", {})
        node["token"] = webui_token.strip()

    ws_token = os.environ.get("DYBOT_WS_TOKEN")
    if ws_token is not None:
        for section, key in _WS_TOKEN_TARGETS:
            cfg.setdefault(section, {})[key] = ws_token.strip()


def load(path=None):
    """读配置：默认 /root/dyonebot/dy-bot/onebot_bridge.config.json。"""
    path = Path(path or os.environ.get("DYBOT_OB_CONFIG")
                or "/root/dyonebot/dy-bot/onebot_bridge.config.json")
    cfg = json.loads(json.dumps(DEFAULTS))     # 深拷贝
    if path.exists():
        try:
            cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except Exception as err:
            raise RuntimeError(f"配置文件解析失败 {path}: {err}") from err
    _apply_env(cfg)
    _apply_special_env(cfg)
    cfg["_config_path"] = str(path)
    return cfg


def ws_token(cfg):
    """当前生效的 OneBot token（三处一致时返回它，不一致则返回主用的）。"""
    forward = ((cfg.get("forward_ws") or {}).get("access_token") or "")
    if forward:
        return forward
    reverse = ((cfg.get("reverse_ws") or {}).get("access_token") or "")
    if reverse:
        return reverse
    return ((cfg.get("http_api") or {}).get("access_token") or "")


def webui_token(cfg):
    return ((cfg.get("webui") or {}).get("token") or "")


def write_default(path=None, force=False):
    """落一份带全部默认值的配置文件，方便用户改。"""
    path = Path(path or "/root/dyonebot/dy-bot/onebot_bridge.config.json")
    if path.exists() and not force:
        return path
    cfg = json.loads(json.dumps(DEFAULTS))
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path
