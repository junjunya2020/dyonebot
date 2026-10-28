# -*- coding: utf-8 -*-
"""抖音 id <-> OneBot id 映射。

OneBot v11 要求 group_id / user_id / self_id 都是 JSON 数字（int64）。
抖音这边有三种 id：

1. 用户 uid            纯数字，19 位以内               -> 直接当 OneBot user_id
2. 群会话 id          纯数字（就是群号）               -> 直接当 OneBot group_id
3. 单聊会话 id        "0:1:<peer>:<me>" 这种字符串     -> 不用它，一律用 peer uid
                                                          （即单聊的 user_id = 对方 uid）

也就是说正常情况下 1:1 恒等映射，不需要查表。但为了不把「将来可能出现的
非数字 id」硬编码成崩溃，这里再留一张持久化的兜底表：任何非数字 id 会被
分配一个 9 开头的合成 int64（高位保留，避免和真实抖音 id 撞车），映射落盘，
重启后不变。

另外维护 uid -> sec_uid / 昵称 的可选缓存，供 get_stranger_info 之类使用。
"""
import json
import os
import threading
from pathlib import Path

# 合成 id 的保留区间：9_000_000_000_000_000_000 起
# 抖音真实 id 目前是 19 位以内（最大 ~9.2e18），这里从 9e18 起会重叠，
# 所以改用「2 开头 + 17 位序号」的区间，远离真实 id 的量级（真实 id 以
# 7 或 3 或 2 开头都可能，因此再叠一层前缀 88 保证不撞）。
_SYNTH_BASE = 8800000000000000000
_SYNTH_RANGE = 100000000000000     # 88_000_000_000_000_000_00 ~ 上限


def is_numeric_id(value):
    """纯数字（可带负号不算）才算能直接当 OneBot id。"""
    s = str(value or "").strip()
    return s.isdigit() and s != ""


def fits_int64(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return False
    return -(2 ** 63) <= v < 2 ** 63


class IdMap:
    """持久化的 id 双射 + 用户资料缓存。"""

    def __init__(self, state_file):
        self.path = Path(state_file)
        self._lock = threading.RLock()
        self._data = {"synth": {}, "profiles": {}, "seq": 0, "messages": [],
                      "self_uid": "", "self_id": 0}
        self._load()

    def register_self(self, self_uid, self_id):
        """登记「机器人自己」的 uid <-> self_id 双射。

        build_self_id() 是用纯函数从抖音 uid 推出来的，不落在 synth 表里，
        所以必须显式登记，否则两个方向都对不上：
          to_onebot(self_uid) 会原样返回抖音 uid（不是 self_id），
          to_douyin(self_id)  也会原样返回 self_id（不是抖音 uid）。
        前者让 @机器人 永远唤不醒 AstrBot，后者会让回 @ 发出一个非法 uid。
        """
        su = str(self_uid or "").strip()
        sid = int(self_id or 0)
        if not su or not sid:
            return
        with self._lock:
            if (str(self._data.get("self_uid") or "") == su
                    and int(self._data.get("self_id") or 0) == sid):
                return
            self._data["self_uid"] = su
            self._data["self_id"] = sid
        self.save()

    # ---------------------------------------------------------- 持久化
    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update({
                    "synth": raw.get("synth") or {},
                    "synth_rev": raw.get("synth_rev") or {},
                    "profiles": raw.get("profiles") or {},
                    "seq": int(raw.get("seq") or 0),
                    "messages": raw.get("messages") or [],
                    "self_uid": str(raw.get("self_uid") or ""),
                    "self_id": int(raw.get("self_id") or 0),
                })
        except Exception:
            # 状态文件损坏不该拦住桥启动，重建即可（映射会变但不会崩）
            pass

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    # ---------------------------------------------------------- 兜底映射
    def to_onebot(self, dy_id):
        """抖音 id -> OneBot int。数字 id 原样返回（int）。"""
        s = str(dy_id or "").strip()
        if s == "":
            return 0
        # 机器人自己：抖音 uid -> OneBot self_id（两者不同区间，必须显式换）
        if s == str(self._data.get("self_uid") or ""):
            sid = int(self._data.get("self_id") or 0)
            if sid:
                return sid
        if s.isdigit() and fits_int64(s):
            return int(s)
        with self._lock:
            hit = self._data["synth"].get(s)
            if hit is not None:
                return int(hit)
            self._data["seq"] = int(self._data["seq"]) + 1
            synth = _SYNTH_BASE + self._data["seq"]
            self._data["synth"][s] = synth
            self._data["synth_rev"] = self._data.get("synth_rev") or {}
            self._data["synth_rev"][str(synth)] = s
            need_save = True
        if need_save:
            self.save()
        return synth

    def to_douyin(self, onebot_id):
        """OneBot int -> 抖音 id 字符串。原样数字就原样回，合成 id 查表。"""
        s = str(onebot_id if onebot_id is not None else "").strip()
        if s == "":
            return ""
        # 机器人自己：OneBot self_id -> 抖音 uid（反向，同上必须显式换）
        sid = int(self._data.get("self_id") or 0)
        if sid and s == str(sid):
            su = str(self._data.get("self_uid") or "")
            if su:
                return su
        with self._lock:
            rev = self._data.get("synth_rev") or {}
            if s in rev:
                return rev[s]
        return s

    # ---------------------------------------------------------- 资料缓存
    def cache_profile(self, uid, nickname="", sec_uid="", avatar="", role=None,
                      group_id=None):
        """缓存用户资料。

        role 是可选的「最后一次见到的群角色」，group_id 给定时同时记一份
        群内角色（sender.role 是群内概念，同一个人在不同群角色可能不同）。
        """
        with self._lock:
            item = self._data["profiles"].setdefault(str(uid), {})
            if nickname:
                item["nickname"] = nickname
            if sec_uid:
                item["sec_uid"] = sec_uid
            if avatar:
                item["avatar"] = avatar
            item.setdefault("nickname", "")
            item.setdefault("sec_uid", "")
            item.setdefault("avatar", "")
            if role is not None:
                item["role"] = role
                if group_id is not None:
                    bygroup = item.setdefault("groups", {})
                    bygroup[str(group_id)] = role

    def get_profile(self, uid):
        with self._lock:
            return self._data["profiles"].get(str(uid)) or {}

    def get_role(self, uid, group_id=None):
        """取群内角色名。群内没记录就退回「最近一次见到的角色」。"""
        with self._lock:
            item = self._data["profiles"].get(str(uid)) or {}
            if group_id is not None:
                got = (item.get("groups") or {}).get(str(group_id))
                if got is not None:
                    return got
            return item.get("role")

    def all_profiles(self):
        with self._lock:
            return dict(self._data["profiles"])

    # ---------------------------------------------------------- 消息索引
    # get_msg / delete_msg 要按 OneBot message_id 反查抖音的
    # (conversation_id, conversation_type, server_message_id)。
    # 只保留最近 N 条，避免状态文件无限增长。
    MAX_MESSAGES = 2000

    def remember_message(self, message_id, conversation_id, conversation_type,
                         server_message_id, sender=0, text="", recalled=False,
                         conversation_short_id=0):
        with self._lock:
            items = self._data.setdefault("messages", [])
            items.append({
                "message_id": int(message_id),
                "conversation_id": str(conversation_id),
                "conversation_type": int(conversation_type),
                "conversation_short_id": int(conversation_short_id or 0),
                "server_message_id": int(server_message_id),
                "sender": int(sender or 0),
                "text": text or "",
                "recalled": bool(recalled),
            })
            if len(items) > self.MAX_MESSAGES:
                del items[:len(items) - self.MAX_MESSAGES]
            self.save()

    def get_conversation_short_id(self, conversation_id, conversation_type):
        """Return the last short id observed in an incoming message."""
        cid = str(conversation_id)
        ctype = int(conversation_type)
        with self._lock:
            for item in reversed(self._data.get("messages") or []):
                if (str(item.get("conversation_id")) == cid
                        and int(item.get("conversation_type") or 0) == ctype):
                    short_id = int(item.get("conversation_short_id") or 0)
                    if short_id:
                        return short_id
        return 0

    def get_message(self, message_id):
        with self._lock:
            for item in reversed(self._data.get("messages") or []):
                if int(item["message_id"]) == int(message_id):
                    return dict(item)
        return None

    def mark_recalled(self, message_id):
        with self._lock:
            for item in self._data.get("messages") or []:
                if int(item["message_id"]) == int(message_id):
                    item["recalled"] = True
                    self.save()
                    return True
        return False
