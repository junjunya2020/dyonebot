# dyonebot — 抖音私信自动回复机器人

> 迁移完成时间：2026-09-25 10:52 UTC
> 原位置：`/root/其他/douyin-spider` + `/root/其他/dy-bot`
> 现位置：`/root/dyonebot/douyin-spider` + `/root/dyonebot/dy-bot`

---

## 1. 迁移摘要

### 目录结构（迁移后）

```
/root/dyonebot/
├── README.md                  ← 本文档
├── douyin-spider/             ← 上游开源项目（源码零改动）
│   ├── .venv/                 ← Python 3.14 虚拟环境（已修复路径）
│   ├── .env                   ← 登录凭据（当前为空，待扫码）
│   ├── .env.example
│   ├── dy_apis/               ← 接口层：登录、收发私信
│   ├── static/                ← protobuf 定义
│   ├── utils/                 ← 工具（load_env 等）
│   ├── builder/ author/ dy_live/ newsign/ tests/
│   └── main.py  quick_publish.py  requirements.txt  Dockerfile
└── dy-bot/                    ← 我自己写的机器人脚本（仓库外）
    ├── start_login.sh         ← 常驻扫码登录循环
    ├── login_qr.py            ← 把二维码渲染成 PNG + 写凭据
    ├── autoreply_bot.py       ← 私信自动回复主程序
    ├── login_qr.png           ← 当前二维码（每分钟覆盖）
    ├── login_loop.log         ← 登录日志
    └── bot_log.txt            ← 机器人日志
```

### 迁移方式

两个目录与原位置 **在同一文件系统**（`dev 64512`），因此使用 `mv` 完成——**原子 rename，毫秒级，438M 无复制**，不存在中途失败留下半个目录的风险。

### 迁移中修复的路径问题

`mv` 本身不修改文件内容，而 venv 内部到处硬编码了绝对路径。共发现并处理 **1755 处**：

| 类别 | 数量 | 处理方式 |
|---|---|---|
| `.venv/bin/` 下脚本 shebang | 17 个文件 | 文本替换 |
| `.venv/pyvenv.cfg` 的 `command=` | 1 | 文本替换 |
| `.venv` 内过期 `.pyc` | 1707 | **删除**（见下方说明） |
| 项目内 `.pyc`（`static/`、`utils/` 等） | 30 | **删除** |
| `dy-bot/*.py`、`*.sh` 硬编码路径 | 11 行 | 文本替换 |

**为什么 `.pyc` 是删除而不是替换：** `.pyc` 是二进制格式，旧的绝对路径被压在 marshalled 的 `co_filename` 里，长度一旦变化就会破坏文件结构。删除是安全的——Python 在首次导入时会按新路径自动重新生成。

**关键排查结论：** 86 个 `.so` 编译扩展**全部没有** RPATH/RUNPATH 指向旧路径（已用 `readelf -d` 逐个确认），所以不存在动态库加载失败的风险。`dist-info/RECORD` 和 `.pth` 文件也均无旧路径引用。

### 迁移后验证（全部通过）

| 检查项 | 结果 |
|---|---|
| git 仓库完整性 | HEAD `4479ea7`，工作区改动 **0**（源码零改动） |
| git 是否把迁移识别为删除 | 否（移到 `.git` 之外，仓库状态不变） |
| venv 自解析路径 | `prefix` = `/root/dyonebot/douyin-spider/.venv` ✓ |
| 关键模块导入 | `douyin_api` / `douyin_recv_msg` / `login_api` / `Live_pb2` / `Response_pb2` / `load_env` 全部成功 |
| `create_conversation` / `send_msg` 存在性 | ✓ |
| pip 可用 | pip 26.2.1，32 个包正常识别 |
| 关键依赖 | qrcode 8.2 / websockets 17.1 / curl_cffi 0.16.3 / protobuf 7.36.2 / av 18.1.0 / numpy 2.5.3 |
| 旧路径残留 | douyin-spider **0** 处，dy-bot 仅剩 1 处历史日志文本 |
| 机器人 dry-run | 正常启动 → 检测无凭据 → 干净退出（退出码 1） |
| 回复规则引擎 | 4 条测试全部返回预期话术 |
| 登录循环从新路径启动 | ✓ 已运行，二维码正常刷新 |

> **唯一保留的"旧路径"**是 `dy-bot/bot_log.txt` 里的一行历史日志文字（`请先完成扫码登录：/root/其他/dy-bot/start_login.sh`）。那是过去某次运行的真实记录，属于历史痕迹而非可执行引用，故不改。

### 项目源码零改动声明

**没有修改任何项目源码。** 所有改动都落在两类对象上：

1. `.venv/` —— 虚拟环境，属于生成物而非源码；
2. `dy-bot/` —— 从一开始就在仓库之外的独立目录。

`git status --porcelain` 输出为空，可作为源码未动的证据。

---

## 2. 快速使用

```bash
# ── 扫码登录（常驻循环，单次窗口 30 分钟到期自动重开，直到扫成功）──
/root/dyonebot/dy-bot/start_login.sh

# 二维码：/root/dyonebot/dy-bot/login_qr.png（每分钟覆盖一次）
# 日志：  /root/dyonebot/dy-bot/login_loop.log
# 停止：  pkill -f start_login.sh

# ── 机器人 dry-run（只打印不发送，安全）──
cd /root/dyonebot/douyin-spider
PYTHONPATH=/root/dyonebot/douyin-spider DYBOT_DRY_RUN=1 \
  .venv/bin/python -u /root/dyonebot/dy-bot/autoreply_bot.py

# ── 正式运行（真实发送；去掉 DYBOT_DRY_RUN）──
cd /root/dyonebot/douyin-spider
PYTHONPATH=/root/dyonebot/douyin-spider \
  .venv/bin/python -u /root/dyonebot/dy-bot/autoreply_bot.py
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DYBOT_QR_TIMEOUT` | `1800` | 单次登录窗口秒数（循环脚本会在到期后自动重开） |
| `DYBOT_DRY_RUN` | 关闭 | 设为 `1` 时只打印回复内容，不真正发送 |

---

## 3. 技术链路（已验证）

```
① 收私信  frontier-im WebSocket
          消息自带 sender / conversation_id / conversation_short_id
                    │
② 换 ticket  消息里【没有】ticket，发送必需
            → DouyinAPI.create_conversation(对方 uid)
            → 返回 (conversation_id, short_id, ticket)
            对已存在会话幂等，会返回同一会话 + 新 ticket
                    │
③ 发回复  DouyinAPI.send_msg(auth, conv_id, short_id, ticket, text)
```

机器人在内存里按对方 uid 缓存 ticket（TTL 300 秒），避免每条消息都重开会话；发送失败时自动清掉缓存，下条消息重新换票。

**回复规则**改 `dy-bot/autoreply_bot.py` 的 `KEYWORD_REPLIES`（关键词 → 话术）和 `DEFAULT_REPLY`（兜底话术）即可，无需动项目源码。

---

## 4. 当前状态与待办

**当前卡点：尚未扫码登录。** `.env` 里 `DY_COOKIES` 为空，因此：

- 机器人会检测到无凭据并立即退出（不会偷偷另起登录流抢二维码）；
- 必须完成扫码后，`DY_TICKET` / `DY_TS_SIGN` / `DY_CLIENT_CERT` / `DY_PRIVATE_KEY` / `DY_DTRAIT_BLOB` 五件套才会一并写入。

**登录循环已在运行并等待扫码。**

### 已知风险预警

日志中有两条 WARNING，扫码后**可能触发二次验证**：

```
未捕获当前浏览器 fpk1，已使用项目内 FingerprintJS fixture 纯算
没有 dtrait 素材（DY_DTRAIT_BLOB / DY_SESSION_DTRAIT 都为空）
  → passport 请求会缺 x-tt-session-dtrait，可能被判需二次验证
```

若真被要求二验，按 `.env.example` 补齐 `DY_DTRAIT_BLOB`（和同设备的 `DY_FPK1` 或对应浏览器组件文件）即可。

---

## 5. 版本信息

| 项 | 值 |
|---|---|
| 项目 HEAD | `4479ea7` |
| Python | 3.14.4（`/usr/bin/python3.14`） |
| 磁盘占用 | douyin-spider 438M（含 venv 433M）+ dy-bot 384K |
| 宿主文件系统 | `/dev/mapper/ubuntu--vg-ubuntu--lv`，剩余 39G |
