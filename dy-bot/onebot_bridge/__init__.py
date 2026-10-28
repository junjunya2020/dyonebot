# -*- coding: utf-8 -*-
"""dyonebot — 把抖音 IM 桥接成 OneBot v11 协议端（正向 + 反向 WS）。

模块划分：
    config.py    配置加载（JSON，支持配置文件 + 环境变量覆盖）
    ids.py       抖音数字 id <-> OneBot int64 id 的稳定双射（持久化）
    cq.py        OneBot 消息段 / CQ 码 / 抖音 content JSON 三者的互转
    dyim.py      抖音 IM 协议层（收发/撤回/群成员/会话/历史），阻塞式，由线程池调用
    actions.py   OneBot v11 action 路由表
    bridge.py    正反向 WebSocket + HTTP API/事件推送 + 事件泵
"""

__version__ = "1.0.0"
