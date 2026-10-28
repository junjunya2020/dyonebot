# 抖音桥 ↔ AstrBot 对接说明

> 结论先行：**AstrBot 完全兼容，无需改动 AstrBot 任何源码或插件**。
> 你的桥只要实现 OneBot v11 反向 WebSocket 的**客户端**一侧即可。
> 本文所有结论均经实测（AstrBot v4.28.1，2026-09-25），非纸面推断。

---

## 0. 一句话架构

```
抖音 App ⇄ frontier-im WebSocket ⇄ 你的桥 ⇄ OneBot v11 反向 WS ⇄ AstrBot
        (douyin-spider 已有)      (在写)     (ws://IP:6199/ws)     (已部署)
```

AstrBot 是 **WS 服务端**，你的桥是 **WS 客户端**（"反向"就是这个意思）。
AstrBot 不需要改，也不需要装插件——它内置 `aiocqhttp` 适配器就是标准 OneBot v11。

---

## 1. 当前状态（已完成的部分）

| 项 | 状态 |
|---|---|
| AstrBot v4.28.1 容器 | 运行中，面板 http://IP:6185 |
| OneBot v11 平台配置 | **已启用**（id=`douyin`，监听 `0.0.0.0:6199`，无 token） |
| 6199 端口监听 | ✓ 已监听（WS 端点） |
| 反向 WS 握手 | ✓ 实测通过 |
| 私聊闭环 | ✓ 实测通过 |
| 群聊闭环 | ✓ 实测通过 |
| 契约自测脚本 | `dy-bot/onebot_contract_test.py`（7/7 通过） |

> 注意：我在 AstrBot 里加了一个平台配置（写进 `cmd_config.json`）。
> 这是**配置**不是源码，原文件已备份为 `cmd_config.json.bak-*`。

---

## 2. 握手：三个 Header，缺一不可

连接 `ws://<AstrBot_IP>:6199/ws`，必须带：

| Header | 值 | 缺了会怎样 |
|---|---|---|
| `X-Client-Role` | `universal` | **HTTP 400 拒绝**（实测） |
| `X-Self-ID` | 桥自编的机器人账号（纯数字） | 连接能成但 API 回调会找不到路由 |
| `User-Agent` | `OneBot/11 xxx` | 建议带，规范要求 |

若 AstrBot 配了 `ws_reverse_token`，还需 `Authorization: Bearer <token>`：
- 不带 → **401**
- 带错 → **403**
- 正确 → 通过
（三种情况均已实测）

**`X-Client-Role` 选哪个：**

| 值 | 含义 | 建议 |
|---|---|---|
| `universal` | 收发同一个连接 | **推荐**，最省事 |
| `event` | 只发事件 | 配合 `api` 用 |
| `api` | 只接 API 回调 | 配合 `event` 用 |

---

## 3. 事件格式（桥 → AstrBot）

### 3.1 私聊消息

```json
{
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend",
  "time": 1790343296,
  "self_id": 7000000000000000001,
  "user_id": 2692024583072670,
  "message_id": 10001,
  "raw_message": "你好",
  "font": 0,
  "sender": {"user_id": 2692024583072670, "nickname": "抖音用户"},
  "message": [{"type": "text", "data": {"text": "你好"}}]
}
```

### 3.2 群聊消息

同上，但 `message_type` 改 `group`，加 `group_id`，`sub_type` 用 `normal`：

```json
{
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "self_id": 7000000000000000001,
  "group_id": 7689446943537234469,
  "user_id": 2692024583072670,
  "message_id": 10002,
  "raw_message": "你好",
  "sender": {"user_id": 2692024583072670, "nickname": "抖音群友", "role": "member"},
  "message": [{"type": "text", "data": {"text": "你好"}}]
}
```

### 3.3 元事件（建议发，让 AstrBot 日志显示"已连接"）

```json
{
  "post_type": "meta_event",
  "meta_event_type": "lifecycle",
  "sub_type": "connect",
  "time": 1790343296,
  "self_id": 7000000000000000001,
  "interval": 500
}
```

### 3.4 三个致命坑（全部实测验证）

**坑 1：`message` 必须是数组，不能是字符串。**
传 `"message": "hi"` 会被 AstrBot 判为非法，它会尝试回你一条错误提示
（走 `action=send_msg`，注意不是 `send_private_msg`），然后这条消息被丢弃。
→ 现代 OneBot 实现都应使用数组格式。

**坑 2：`sub_type` 不要乱写。**
AstrBot 把事件名拼成 `message.private.<sub_type>`，靠逐级向上剥离来匹配 handler
（`bus.py` 的 `emit`）。私聊写 `friend`、群聊写 `normal` 最稳。

**坑 3：ID 必须是 JSON number，且不能超 int64。**
AstrBot 把 uid 当数字解析（`int(...)`）。抖音 uid 量级约 2.6e15，
群号约 7.7e18——**都在 int64 上限 9.22e18 以内**，安全。
实测 `user_id: 2692024583072670` 往返后类型仍是 `int`，保真。
→ 千万别把 uid 当字符串塞进本该是数字的字段，也别做字符串截断。

**坑 4：`conversation/list` 的 `limit` 超过 50 会静默回空（2026-09 真机实测）。**

`cmd 2006 /v1/conversation/list` 用 `limit` 控制页大小，边界正好卡在 50：

| limit | 返回 |
|---|---|
| 48 / 49 / 50 | 正常回页 |
| **51** | **n=0** |
| 100 / 1000 / 2³¹-1 | n=0 |

回空时 `msg="OK"`、`error_desc` 为空、`has_more=False`、`total=0` —— 服务器收下了请求，
当成「没有任何会话」回给你，**不报错**，所以调用方没法从返回值区分"真没会话"和"limit 太大"。
单聊（`con_type=1`）和群聊（`con_type=2`）表现一致。

已排除的旁路：整数溢出（proto 里 `limit` 是 `int32`，`2³¹` 才在本地报
`ValueError: Value out of range`，`2³¹-1` 仍然回空）、`with_cold`/`sort_type` 取 0~3 的
各种组合（`limit=100` 下全为空）。**无解，只能自己分页。**

`cursor` 是**偏移量**而不是 token：`cursor=0` 回前 5 条，`1` 回 4 条，`2` 回 3 条，
`3` 回 2 条，`5` 回空。

→ 桥内 `DouyinIM.conversation_list()` 已把每页夹到 ≤50（`CONVERSATION_PAGE_MAX`）
并自己翻页取满，调用方传 `limit=200` 也是安全的。
注意这个接口只用于**枚举类**能力（`get_group_list` / `get_friend_list` / 撤回对账 / 昵称预取），
**消息收发完全不走它**，走的是 `dyim.py` 的 `send_message` 和 WS 推送。

---

## 4. API 调用（AstrBot → 桥）

AstrBot 通过**同一个连接**发指令，桥必须回执：

```json
// AstrBot 发来
{"action": "send_private_msg",
 "params": {"user_id": 2692024583072670, "message": [{"type":"text","data":{"text":"你好"}}]},
 "echo": {"seq": 1}}

// 桥必须回（echo 原样带回，这是匹配依据）
{"status": "ok", "retcode": 0, "data": {"message_id": 20001}, "echo": {"seq": 1}}
```

**`echo` 必须原样回传**——AstrBot 用 `echo.seq` 在 `ResultStore` 里找对应的 future。
不回 = 挂起，直到 `api_timeout_sec`（AstrBot 设的是 **180 秒**）超时。

### 4.1 桥必须实现的 action（最小集）

| action | 用途 | 建议 |
|---|---|---|
| `send_private_msg` | 发私信 | **必须** |
| `send_group_msg` | 发群消息 | 用群聊就必须 |
| `send_msg` | 通用发消息（靠 `message_type` 区分） | 建议顺手实现，AstrBot 错误提示走这条 |

### 4.2 AstrBot 可能调用、但你可以不实现的 action

实测确认：**回 `status: failed` 是完全安全的**，AstrBot 不会崩溃，链路继续可用。
（我做过对照实验：对未实现 action 一律回 failed 后，紧接着发下一条消息，AstrBot 正常处理。）

| action | 触发条件 | 不实现的影响 |
|---|---|---|
| `get_msg` | 消息带 `reply` 引用段 | 引用消息内容取不到，消息仍能收到 |
| `get_group_member_info` | 消息带 `at` 段 | @ 的昵称显示为空，其余正常 |
| `get_stranger_info` | 同上（查不到时兜底） | 同上 |
| `get_group_info` | 部分群相关功能 | 群名/人数缺失 |
| `get_private_file_url` / `get_group_file_url` | 收到文件段 | 文件消息取不到 URL |
| `get_image` / `get_file` / `get_forward_msg` / `get_group_member_list` | 各功能按需 | 对应功能降级 |
| `send_private_forward_msg` / `send_group_forward_msg` | 长消息合并转发 | 超长回复可能失败 |

**回 failed 的标准格式：**
```json
{"status": "failed", "retcode": 1404, "msg": "not implemented", "echo": {"seq": 1}}
```

> ⚠️ 千万别"沉默不回"——那会卡满 180 秒超时，表现为 AstrBot 明显卡顿。

---

## 5. 抖音需要特别处理的字段映射

你的链路（`autoreply_bot.py` 已验证）与 OneBot 的对应关系：

| OneBot 字段 | 抖音来源 | 备注 |
|---|---|---|
| `user_id` | `msg.sender` | 数字，直接用 |
| `group_id` | `msg.conversation_id` | 群聊时；单聊留空 |
| `message_id` | `msg.server_message_id` | 用于撤回（你已有 `recall`） |
| `sender.nickname` | `dyim_client.get_profile(sec_uid)` | 有缓存，注意**别在事件里同步阻塞** |
| `message[].data.text` | `json.loads(msg.content)["text"]` | `message_type == 7` 才是文本 |

**反向（`send_private_msg` → 抖音）：**
需要复刻你现在 `autoreply_bot.reply()` 的两步：
1. `DouyinAPI.create_conversation(auth, peer_uid)` 换 ticket（你已有 300s 缓存）
2. `DouyinAPI.send_msg(auth, conv_id, short_id, ticket, text)`

**关键设计提醒：**
- `create_conversation` 是**同步阻塞**的 HTTP 调用。放在 WS 回调里直接跑会卡住事件循环，
  建议丢到线程池（`asyncio.to_thread`）再回 `echo`。
- 或者：先立即回 `{"status":"ok"}`，再异步真正发送。
  OneBot 语义上 `send_*` 应该发送成功后才回，但对桥来说先生效更实用——
  因为不回会卡 180s，而抖音发送偶尔会慢。
- `message_id` 回填：抖音发送后返回的 id 建议填进 `data.message_id`，
  否则 AstrBot 某些需要撤回/引用的功能会拿不到。

---

## 6. 配置 AstrBot 侧（已帮你配好）

我已在 `data/cmd_config.json` 写入：

```json
"platform": [{
  "id": "douyin",
  "type": "aiocqhttp",
  "enable": true,
  "ws_reverse_host": "0.0.0.0",
  "ws_reverse_port": 6199,
  "ws_reverse_token": ""
}]
```

改完后 `docker compose restart astrbot` 生效。

> 面板里也能图形化改：**机器人 → 创建机器人 → 选「OneBot v11」**。

---

## 7. 自测脚本

```bash
cd /root/dyonebot/douyin-spider
.venv/bin/python /root/dyonebot/dy-bot/onebot_contract_test.py
```

覆盖 7 项：握手、角色头校验、token 校验、私聊闭环、群聊闭环、
ID 保真、未实现 action 容错、message 格式校验。

若你把 AstrBot 配了 token，把脚本顶部 `TOKEN = ""` 填上再跑。

---

## 8. 建议的落地顺序

1. **先跑通最小闭环**：桥只实现 `send_private_msg` + 发私聊事件，
   用 `onebot_contract_test.py` 确认 AstrBot 能收到并回复。
2. **接真实抖音**：把 `autoreply_bot.py` 里的 `on_message` 改成"发 WS 事件"，
   把 `reply()` 改成"响应 `send_private_msg` 回调"。
3. **再接群聊**：加 `group_id` 映射 + `send_group_msg`。
4. **最后补昵称**：接 `get_profile`（注意异步化，别阻塞事件循环）。

**你现在的 `autoreply_bot.py` 是"自己决定回复内容"；
接 AstrBot 后，回复内容由 AstrBot 决定**（大模型/插件/规则），
桥退化成纯粹的协议转换层——这是正确的分工。

---

## 9. 还需要你确认的

1. **AstrBot 里还没配大模型**。实测时 AstrBot 回了
   `LLM 请求失败：未找到任何可用的对话模型（提供商）`。
   要真正对话，需在面板配置一个 Provider（或先只用内置命令如 `/help` 验证）。
2. **抖音登录态**：`.env` 里 `DY_COOKIES` 目前为空，
   需要先扫码登录，桥才能收发真实消息。
