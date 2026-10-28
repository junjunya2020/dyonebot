# dyonebot

**把抖音私信 / 群聊实时桥接成 [OneBot v11](https://github.com/botuniverse/onebot-11) 标准协议。**

它的定位是一个**协议转换层**：抖音不再是"又一个要单独对接的野路子 SDK"，而变成 OneBot 生态里的一个普通平台端。接上它之后，**AstrBot / NoneBot2 / NapCat / Koishi 等任何支持 OneBot v11 的机器人框架，都能直接收发抖音消息**——回复内容交给框架（大模型 / 插件 / 规则），本桥只负责把两侧的协议对上。

```
抖音 App  ⇄  抖音 IM WebSocket  ⇄  dyonebot  ⇄  OneBot v11  ⇄  你的机器人框架
          (douyin-spider 协议层)    (本桥)     (WS / HTTP)     (AstrBot 等)
```

> 本项目是个人自用工具开源，**非官方、与抖音无任何关联**。请勿用于批量营销、骚扰、发布违法不良信息。详见文末免责声明。

---

## 目录

- [特性](#特性)
- [架构](#架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [支持的 OneBot v11 Action](#支持的-onebot-v11-action)
- [对接 AstrBot](#对接-astrbot)
- [已知限制](#已知限制)
- [免责声明与致谢](#免责声明与致谢)

---

## 特性

- **一套事件流，四种通信方式**，均可独立开关：
  - **正向 WebSocket** — 本桥监听端口，OneBot 客户端连进来
  - **反向 WebSocket** — 本桥主动连对方（AstrBot 的 `aiocqhttp` 默认走这条）
  - **HTTP API** — `POST /<action>` 同步返回结果
  - **HTTP POST** — 把事件上报到你指定的 URL
- **消息类型**：私聊 / 群聊，文本、图片、@、回复引用、撤回，支持 **CQ 码** 与 **消息段数组** 两种格式互转
- **抖音加密图片自动解密**：抖音私信图片是 AES-256-GCM 密文，本桥内置媒体服务解密后转成可直接访问的 HTTP 图片地址
- **ID 双射映射**：把抖音 uid / 群号映射成 int64 范围内的 OneBot ID，`@机器人`、`@群成员`、撤回都能正确还原
- **WebUI 控制面板**：网页扫码登录、上游连接状态、实时日志、配置查看
- **多级自动重连**：上游 WS 断线自动重连 + 心跳保活，`systemd` 崩溃自动拉起

## 架构

桥内部按职责拆成若干模块（`dy-bot/onebot_bridge/`）：

| 模块 | 职责 |
|---|---|
| `bridge.py` | OneBot 适配层：正向 / 反向 WS、HTTP API、HTTP POST 事件上报 |
| `actions.py` | OneBot v11 action 路由表（按方法名分发，见下方 action 清单） |
| `cq.py` | 消息段 / CQ 码 / 抖音 content JSON 三者互转 |
| `dyim.py` | 抖音 IM 协议层：收发、撤回、群成员、会话、历史、资料 |
| `ids.py` | 抖音 ID ⇄ OneBot ID 的 int64 双射映射 |
| `media.py` | 抖音加密图片解密 + 对外 HTTP 媒体服务 |
| `upstream.py` | 抖音推送 WS 看护：多级重连 + 状态快照 |
| `login.py` / `webui.py` | 扫码登录与网页控制面板 |
| `config.py` / `logutil.py` | 配置加载、日志中枢（环形缓冲，供面板读取） |

底层抖音协议来自开源项目 [`cv-cat/Douyin_Spider`](https://github.com/cv-cat/Douyin_Spider)，位于 `douyin-spider/`，**源码基本未改动**。

## 目录结构

```
dyonebot/
├── dy-bot/                    ← 本项目自研部分（OneBot 桥）
│   ├── onebot_bridge/         ← 桥的核心包（见上表）
│   ├── run_bridge.py          ← 启动入口：读配置 → 建抖音连接 → 跑桥
│   ├── onebot_bridge.config.json   ← 桥配置文件
│   ├── start_login.sh         ← 常驻扫码登录循环
│   ├── restart_bridge.sh      ← 重启桥
│   ├── onebot_contract_test.py    ← OneBot v11 契约自测（7 项）
│   └── onebot_bridge_selftest.py  ← 端到端自测
├── douyin-spider/             ← 抖音协议层（上游开源，私信收发 / 登录 / 直播）
│   ├── dy_apis/               ← 接口层
│   ├── utils/ builder/        ← 工具与请求签名
│   ├── static/                ← protobuf 定义
│   └── requirements.txt
├── ONEBOT-对接说明.md         ← 与 AstrBot 对接的实测结论与踩坑记录（强烈建议先看）
└── README.md
```

> ⚠️ 仓库内的 `start_login.sh` / `restart_bridge.sh` 里的路径默认按 `/root/dyonebot/` 部署编写，部署到别处请自行修改其中的 `REPO` 等变量。

## 快速开始

### 环境要求

- Linux（systemd），Python **3.10+**（实测 3.14）
- 一个可用的抖音账号（用于扫码登录）

### 1. 安装

```bash
git clone https://github.com/junjunya2020/dyonebot.git
cd dyonebot/douyin-spider

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

> 注意：`protobuf` 需 ≥ 5.27。`blackboxprotobuf` 声明依赖旧版 protobuf，装完请确认 protobuf 没有被降级，否则会报 `ImportError: cannot import name 'runtime_version'`。

### 2. 扫码登录

先复制一份配置模板，再启动扫码：

```bash
# 按需修改 dy-bot/onebot_bridge.config.json（见「配置说明」）
cp douyin-spider/.env.example douyin-spider/.env

# 方式 A：网页面板扫码（推荐）——启动桥后打开 http://<本机IP>:6800/
# 方式 B：命令行常驻扫码，二维码刷成 PNG
./dy-bot/start_login.sh
#   二维码文件：dy-bot/login_qr.png
#   停止：     pkill -f start_login.sh
```

扫码成功后，`douyin-spider/.env` 会自动写入 `DY_COOKIES` 及 `DY_TICKET` / `DY_TS_SIGN` / `DY_CLIENT_CERT` / `DY_PRIVATE_KEY` / `DY_DTRAIT_BLOB` 等签名凭据。

### 3. 运行

```bash
cd douyin-spider
.venv/bin/python -u ../dy-bot/run_bridge.py
```

或注册为 systemd 服务（示例）：

```ini
[Unit]
Description=dyonebot — 抖音 OneBot v11 桥
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/dyonebot/douyin-spider
ExecStart=/root/dyonebot/douyin-spider/.venv/bin/python -u /root/dyonebot/dy-bot/run_bridge.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now dyonebot-bridge
```

## 配置说明

配置文件 `dy-bot/onebot_bridge.config.json`，优先级：**环境变量 > 配置文件 > 内置默认值**。

| 字段 | 说明 |
|---|---|
| `reverse_ws.urls` | 反向 WS 地址列表，指向你的机器人框架（如 AstrBot `ws://<IP>:8848/ws`）；`access_token` 是握手令牌 |
| `forward_ws` | 正向 WS 监听（默认 `127.0.0.1:6700`），供客户端连入 |
| `http_api` | HTTP API 监听（默认 `127.0.0.1:6701`），`POST /<action>` |
| `http_post` | 事件 HTTP 上报，默认关闭 |
| `recall_poll` | 撤回轮询（抖音无撤回推送，靠轮询对账） |
| `profile_prefetch` | 启动时预取好友 / 群成员昵称 |
| `media` | 加密图片解密服务：`public_base` / `port`(6801) / `ttl_sec` |
| `douyin_ws` | 抖音上游 WS 心跳与重连参数 |
| `webui` | 控制面板监听与端口（默认 6800） |

> 仓库内配置中的 `access_token` 均为占位符 `CHANGE_ME`，**部署时请全部替换为你自己的随机令牌**。

## 支持的 OneBot v11 Action

桥已实现下列 OneBot v11 action（`actions.py` 按方法名分发）：

| 分类 | Action |
|---|---|
| 发送消息 | `send_msg` · `send_private_msg` · `send_group_msg` · `send_private_forward_msg` · `send_group_forward_msg` |
| 消息操作 | `get_msg` · `get_forward_msg` · `delete_msg` |
| 账号 / 好友 | `get_login_info` · `get_stranger_info` · `get_friend_list` · `send_like` · `set_friend_add_request` |
| 群管理 | `get_group_info` · `get_group_list` · `get_group_member_info` · `get_group_member_list` · `get_group_msg_history` · `get_group_honor_info` · `get_group_system_msg` · `set_group_ban` · `set_group_whole_ban` · `set_group_card` · `set_group_kick` · `set_group_leave` · `set_group_name` |
| 群文件 | `get_group_file_system_info` · `get_group_root_files` · `get_group_file_url` · `get_private_file_url` |
| 媒体 | `get_image` · `get_record` · `get_file` · `can_send_image` · `can_send_record` |
| 系统 | `get_status` · `get_version_info` · `get_online_clients` · `get_csrf_token` · `set_restart` |

> 受抖音官方接口能力所限，**部分 action 只能降级返回**（例如历史消息、群文件、部分群资料）。未实现 / 不可用的 action 一律按协议规范显式返回 `failed`（`retcode 1404`），**不会沉默挂起**。具体可用性见 `ONEBOT-对接说明.md`。

## 对接 AstrBot

AstrBot 内置的 `aiocqhttp` 适配器就是标准 OneBot v11，**无需改任何源码或插件**，把本桥当成它的一个平台端即可。

握手要点（三个 Header 缺一不可）：

| Header | 值 |
|---|---|
| `X-Client-Role` | `universal`（推荐，收发同一条连接） |
| `X-Self-ID` | 桥自编的机器人账号（纯数字） |
| `User-Agent` | `OneBot/11 xxx` |

若框架侧配了 `ws_reverse_token`，需额外带 `Authorization: Bearer <token>`。

完整的字段映射、事件格式、七个实测坑（`message` 必须是数组、`sub_type` 取值、int64 边界、`conversation/list` 的 `limit≤50` 等）全部记录在 **[`ONEBOT-对接说明.md`](./ONEBOT-对接说明.md)**，接之前务必读一遍。

自测：

```bash
cd douyin-spider
.venv/bin/python ../dy-bot/onebot_contract_test.py   # 7 项契约自测：握手 / 私聊 / 群聊 / ID 保真 / 容错
```

## 已知限制

- **抖音登录态会过期**，需要重新扫码；上游 `mssdk` 会定期轮换 `msToken` 保活，但长期仍需重登。
- 抖音私信图片是**加密传输**，必须经本桥的媒体服务解密，不能直接把原始 URL 丢给客户端。
- 抖音**没有撤回推送**，撤回靠轮询对账（有延迟）。
- 部分 OneBot action 受抖音接口能力限制只能降级或不可用（见上）。
- 仅面向**单账号**；多账号需起多实例。
- 抖音协议随时可能变动，上游 `douyin-spider` 若不跟进则可能失效。

## 免责声明与致谢

- 本项目**仅供学习与技术研究**，请勿用于批量营销、骚扰、诈骗或任何违法用途。使用产生的一切后果由使用者自负。
- 底层抖音协议实现来自开源项目 **[cv-cat/Douyin_Spider](https://github.com/cv-cat/Douyin_Spider)**（见 `douyin-spider/` 及其原始 README 与许可证）。若涉及侵权请联系上游作者。
- OneBot v11 协议规范见 [botuniverse/onebot-11](https://github.com/botuniverse/onebot-11)。

---

<sub>本项目为个人自用工具整理开源 · 由 [@junjunya2020](https://github.com/junjunya2020) 维护 · 欢迎 Issue / PR</sub>
