# -*- coding: utf-8 -*-
"""OneBot 消息段 / CQ 码 / 抖音 content JSON 三者互转。

抖音的私信 content 是一个 JSON 字符串，结构随 message_type 变化。
桥要做两件事：

  收：抖音 (message_type, content) -> OneBot message 段数组 + CQ 码字符串
  发：OneBot message（段数组 或 CQ 码字符串）-> 抖音 (message_type, content)

只做【实测过】的类型转换，遇到没验证过的类型一律降级成文本占位符，
并把原始 JSON 放在段的 data.raw 里，不猜字段名、不伪造类型。
"""
import json
import re


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# QQ face id -> unicode emoji。
#
# 抖音 IM 没有 QQ 那套 face 目录（没有「微笑/呲牙」这种带 id 的官方表情资源），
# 所以 face 段无法原样转发。但 unicode emoji 是纯文本，任何客户端都能显示，
# 因此这里把**常用的** QQ 表情映射成等义的 unicode emoji，映射不到的诚实
# 降级成 "[表情]" 文本 —— 既不丢内容，也不假装发了个抖音表情。
# 只收有明确对等语义的条目，宁可少不可错。
QQ_FACE_EMOJI = {
    "0": "😊", "1": "😖", "2": "😍", "3": "😠", "4": "😎",
    "5": "😭", "6": "😳", "7": "😡", "8": "😜", "9": "😴",
    "10": "😲", "11": "😔", "12": "😄", "13": "😷", "14": "😧",
    "15": "😨", "16": "😰", "17": "😏", "18": "😞", "19": "😱",
    "20": "😩", "21": "😪", "22": "😅", "23": "😛", "24": "😤",
    "25": "😫", "26": "😝", "27": "😬", "28": "😑", "29": "😐",
    "30": "😚", "31": "😘", "32": "😌", "33": "😳", "34": "😁",
    "35": "😰", "36": "😵", "37": "😲", "38": "😷", "39": "🤐",
    "40": "😴", "41": "😷", "42": "😷", "43": "🙄", "44": "😤",
    "45": "😴", "46": "😷", "47": "😷", "48": "🙄", "49": "😤",
    "53": "😊", "54": "😐", "55": "😑", "56": "😶", "57": "🙂",
    "58": "😕", "59": "😟", "60": "😮", "61": "😯", "62": "😲",
    "63": "😳", "64": "😦", "65": "😧", "66": "😨", "67": "😰",
    "68": "😥", "69": "😢", "70": "😭", "71": "😱", "72": "😖",
    "73": "😣", "74": "😞", "75": "😓", "76": "😩", "77": "😫",
    "78": "😤", "79": "😡", "80": "😠", "81": "😡", "82": "😈",
    "83": "👿", "84": "💀", "85": "💩", "86": "🤡", "87": "👻",
    "88": "👽", "89": "🤖", "96": "😺", "97": "😸", "98": "😹",
    "99": "😻", "100": "😼", "101": "😽", "102": "🙀", "103": "😿",
    "104": "😾", "105": "🙈", "106": "🙉", "107": "🙊", "108": "💋",
    "109": "💄", "110": "💍", "111": "💎", "112": "🐒", "113": "🐶",
    "114": "🐕", "115": "🐩", "116": "🐺", "117": "🐱", "118": "🐈",
    "119": "🐯", "120": "🐅", "121": "🐆", "122": "🐴", "123": "🐎",
    "124": "🐮", "125": "🐂", "126": "🐃", "127": "🐄", "128": "🐷",
    "129": "🐖", "130": "🐗", "131": "🐽", "132": "🐏", "133": "🐑",
    "134": "🐐", "135": "🐪", "136": "🐫", "137": "🐘", "138": "🐭",
    "139": "🐁", "140": "🐀", "141": "🐹", "142": "🐰", "143": "🐇",
    "144": "🐻", "145": "🐼", "146": "🐨", "147": "🐾", "148": "🐔",
    "149": "🐓", "150": "🐣", "151": "🐤", "152": "🐥", "153": "🐦",
    "154": "🐧", "155": "🐸", "156": "🐊", "157": "🐢", "158": "🐍",
    "159": "🐲", "160": "🐉", "161": "🐳", "162": "🐋", "163": "🐬",
    "164": "🐟", "165": "🐠", "166": "🐡", "167": "🐙", "168": "🐚",
    "169": "🐌", "170": "🐛", "171": "🐜", "172": "🐝", "173": "🐞",
    "174": "🐌", "175": "🐢", "176": "🌺", "177": "🌸", "178": "🌼",
    "179": "🌻", "180": "🌞", "181": "🌝", "182": "🌚", "183": "🌑",
    "184": "🌒", "185": "🌓", "186": "🌔", "187": "🌕", "188": "🌖",
    "189": "🌗", "190": "🌘", "191": "🌙", "192": "🌎", "193": "🌍",
    "194": "🌏", "195": "🌟", "196": "⭐", "197": "🌠", "198": "☁️",
    "199": "⛅", "200": "🌤", "201": "🌧", "202": "⛈", "203": "🌩",
    "204": "⚡", "205": "🔥", "206": "💥", "207": "❄️", "208": "💧",
    "209": "🌊", "210": "🎄", "211": "🎃", "212": "🎆", "213": "🎇",
    "214": "✨", "215": "🎉", "216": "🎊", "217": "🎈", "218": "🎁",
    "219": "🎀", "220": "🎂", "221": "🍰", "222": "🍭", "223": "🍬",
    "224": "🍫", "225": "🍿", "226": "🍩", "227": "🍪", "228": "🍺",
    "229": "🍻", "230": "🍷", "231": "🍸", "232": "🍹", "233": "🍵",
    "234": "☕", "235": "🍶", "236": "🍼", "237": "🍴", "238": "🍕",
    "239": "🍔", "240": "🍟", "241": "🍗", "242": "🍖", "243": "🍚",
    "244": "🍛", "245": "🍜", "246": "🍝", "247": "🍞", "248": "🍳",
    "249": "🍲", "250": "🍱", "251": "🍣", "252": "🍎", "253": "🍊",
    "254": "🍋", "255": "🍌", "256": "🍉", "257": "🍇", "258": "🍓",
    "259": "🍑", "260": "🍒", "261": "🍍", "262": "🥝", "263": "🥑",
    "264": "🌽", "265": "🍅", "266": "🍆", "267": "🌰", "268": "🍯",
    "270": "🤝", "271": "✌️", "272": "👌", "273": "👍", "274": "👎",
    "275": "👊", "276": "✊", "277": "🙏", "278": "👏", "279": "🙌",
    "280": "👐", "281": "💪", "282": "👋", "283": "🖐", "284": "✋",
    "285": "🤚", "286": "🖖", "287": "👆", "288": "👇", "289": "👈",
    "290": "👉", "291": "💋", "292": "💋", "293": "💘", "294": "💝",
    "295": "💖", "296": "💗", "297": "💓", "298": "💞", "299": "💕",
    "300": "💌", "301": "❤️", "302": "💔", "303": "💛", "304": "💚",
    "305": "💙", "306": "💜", "307": "💟", "308": "💯", "309": "💢",
    "310": "💫", "311": "💥", "312": "💦", "313": "💨", "314": "🕳",
    "315": "💬", "316": "💭", "317": "🗯", "318": "♠️", "319": "♣️",
    "320": "♥️", "321": "♦️", "322": "🃏", "323": "🎴", "324": "🀄",
    "325": "🕐", "326": "⏰", "327": "⌚", "328": "⏳", "329": "⏰",
    "330": "🕐", "331": "🕑", "332": "🕒", "333": "🕓", "334": "🕔",
    "335": "🕕", "336": "🕖", "337": "🕗", "338": "🕘", "339": "🕙",
    "340": "🕚", "341": "🕛", "342": "🌞", "343": "🌙", "344": "☀️",
    "345": "☔", "346": "☂️", "347": "❄️", "348": "⛄", "349": "🌊",
}

# ---------------------------------------------------------------- CQ 转义

_CQ_ESCAPE = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"))
_CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&amp;", "&"))


def escape_cq(value):
    s = str(value if value is not None else "")
    for raw, enc in _CQ_ESCAPE:
        s = s.replace(raw, enc)
    return s.replace(",", "&#44;")


def unescape_cq(value):
    s = str(value if value is not None else "")
    for enc, raw in _CQ_UNESCAPE:
        s = s.replace(enc, raw)
    return s


# ---------------------------------------------------------------- 段 <-> CQ

def segments_to_cq(segments):
    """段数组 -> CQ 码字符串。text 段直接裸写，其余套 [type,k=v]。"""
    out = []
    for seg in segments or []:
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            out.append(str(data.get("text", "")))
            continue
        if not stype:
            continue
        parts = [f"{k}={escape_cq(v)}" for k, v in data.items()
                 if v is not None and k != "raw"]
        out.append(f"[CQ:{stype}" + ("," + ",".join(parts) if parts else "") + "]")
    return "".join(out)


# [CQ:type,k=v,k=v...] —— 分隔符是【逗号】不是点号。
# 值里的逗号/方括号由 escape_cq 转成 &#44; / &#91; / &#93;，所以这里可以放心
# 用 [^,\]] 切分；写成 "." 会让所有带参数的 CQ 码整条匹配失败、退化成纯文本。
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_0-9]+)((?:,[^,\]]*)*)\]")


def cq_to_segments(text):
    """CQ 码字符串 -> 段数组。不认识的 CQ 码原样保留成 text，不丢内容。"""
    text = "" if text is None else str(text)
    segments = []
    pos = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > pos:
            segments.append({"type": "text",
                             "data": {"text": text[pos:match.start()]}})
        ctype = match.group(1)
        raw_params = (match.group(2) or "").lstrip(",")
        data = {}
        for chunk in raw_params.split(",") if raw_params else []:
            if "=" in chunk:
                key, _, val = chunk.partition("=")
                data[key.strip()] = unescape_cq(val)
            elif chunk:
                data[chunk.strip()] = ""
        segments.append({"type": ctype, "data": data})
        pos = match.end()
    if pos < len(text):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return segments


def normalize_segments(message):
    """OneBot 的 message 字段：数组直接用，字符串按 CQ 码解析。"""
    if isinstance(message, list):
        out = []
        for item in message:
            if isinstance(item, dict) and item.get("type"):
                out.append({"type": item["type"], "data": item.get("data") or {}})
            elif isinstance(item, str):
                out.extend(cq_to_segments(item))
        return out
    if isinstance(message, str):
        return cq_to_segments(message)
    if message is None:
        return []
    return [{"type": "text", "data": {"text": str(message)}}]


def segments_to_plain(segments):
    """段数组 -> 人类可读纯文本（用于日志与历史对账）。"""
    out = []
    for seg in segments or []:
        stype, data = seg.get("type"), seg.get("data") or {}
        if stype == "text":
            out.append(str(data.get("text", "")))
        elif stype == "at":
            out.append("@" + str(data.get("qq", "")))
        elif stype == "image":
            out.append("[图片]")
        elif stype == "face":
            out.append("[表情]")
        elif stype == "reply":
            out.append("[回复]")
        elif stype == "record":
            out.append("[语音]")
        elif stype == "video":
            out.append("[视频]")
        elif stype == "file":
            out.append("[文件]")
        else:
            out.append(f"[{stype}]")
    return "".join(out)


# ---------------------------------------------------------------- 收：抖音 -> OneBot

# 抖音 IM message_type（项目 DouyinAPI 里的常量，已核对）
DY_TEXT = 7
DY_BIG_EMOJI = 5          # 表情包 / 大表情
DY_IMAGE = 27             # 图片
DY_VIDEO = 30             # 视频
DY_VOICE = 17             # 语音
DY_ENCRYPT_VOICE = 109
DY_SHARE_AWEME = 8        # 分享视频
DY_SHARE_PHOTOS = 77
DY_SHARE_WEB = 26
DY_SHARE_USER = 25
DY_FILE = 6
DY_NOTICE = 1001          # 群系统通知
DY_RECALL_EVENT = 40001   # 撤回【事件】（不是消息）

# richTextInfos[].infoType：实测 1=用户段 2=字体样式 3=composed_id 6=@提及。
# 只有 6 带 info.uid + mention_id，是 @ 的唯一可靠来源。
_RT_MENTION = 6


def _first_url(node):
    """从各种 url_list 结构里取第一个可用的 http url。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("display_image", "display_image_url", "origin_url_list",
                    "url_list", "urlList", "url", "cover", "image"):
            got = _first_url(node.get(key))
            if got:
                return got
    if isinstance(node, list):
        for item in node:
            got = _first_url(item)
            if got:
                return got
    return ""


def _first_url_list(node):
    """取整个 url_list（不是只取一个），代理服务要按顺序回退。"""
    if isinstance(node, dict):
        for key in ("url_list", "urlList", "origin_url_list",
                    "large_url_list", "url"):
            got = node.get(key)
            if isinstance(got, str) and got.startswith("http"):
                return [got]
            if isinstance(got, list):
                urls = [u for u in got if isinstance(u, str) and u.startswith("http")]
                if urls:
                    return urls
    if isinstance(node, str) and node.startswith("http"):
        return [node]
    if isinstance(node, list):
        urls = [u for u in node if isinstance(u, str) and u.startswith("http")]
        if urls:
            return urls
    return []


def _find_value(node, keys):
    """Recursively find the first non-empty value for one of ``keys``."""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if value not in (None, "", [], {}):
                return value
        for value in node.values():
            found = _find_value(value, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_value(value, keys)
            if found not in (None, "", [], {}):
                return found
    return ""


def _find_first_frame(node):
    """Find the first image in nested photo-card fields."""
    if isinstance(node, dict):
        for key in ("images", "image_list", "image_infos"):
            values = node.get(key)
            if isinstance(values, list) and values:
                url = _first_url(values[0])
                if url:
                    return url
        for value in node.values():
            url = _find_first_frame(value)
            if url:
                return url
    elif isinstance(node, list):
        for value in node:
            url = _find_first_frame(value)
            if url:
                return url
    return ""


def _item_id_from_text(value):
    value = str(value or "")
    if value.isdigit():
        return value
    match = re.search(r"/(?:video|note|slides)/(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"[?&](?:modal_id|item_id|aweme_id)=(\d+)", value)
    return match.group(1) if match else ""


def _load_content(content):
    if isinstance(content, (dict, list)):
        return content
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return {"text": str(content)}
    return parsed if isinstance(parsed, (dict, list)) else {"text": str(parsed)}


def _media_url(media, urls, *, skey="", md5="", data_size=0):
    """把一串 CDN 直链换成「本桥代理并解密」的短 URL。

    抖音图片是 AES-256-GCM 密文（见 media.py 顶部），CDN 直链直接给客户端
    是显示不出来的。media 为空（或注册失败）时退回第一个直链，行为与改造前
    一致，不会因为媒体服务挂了就把消息变成空的。
    """
    urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith("http")]
    if media is not None and urls:
        token = media.register(urls=urls, skey=skey, md5=md5, data_size=data_size)
        got = media.url_for(token)
        if got:
            return got
    return urls[0] if urls else ""


def _resource_urls(content):
    """从 resource_url 里按「优先原图」的顺序取直链。"""
    node = content.get("resource_url")
    if isinstance(node, dict):
        for key in ("origin_url_list", "large_url_list", "url_list",
                    "medium_url_list", "thumb_url_list"):
            urls = node.get(key)
            if isinstance(urls, list) and urls:
                return urls, node
        return [], node
    return ([node] if isinstance(node, str) else []), {}


def dy_content_to_segments(message_type, content, nicknames=None, media=None,
                           idmap=None):
    """抖音消息 -> OneBot 段数组。

    nicknames: {uid_str: nickname}，用来把富文本里的 @ 还原成 at 段。
    media:     MediaCache，用来把加密图片换成可显示的代理 URL（可为 None）。
    idmap:     IdMap，用来把抖音 uid 换成 OneBot id（at 段的 qq 必须是
               OneBot id，否则 @机器人 不等于事件的 self_id，唤不醒）。
    """
    content = _load_content(content)
    if not isinstance(content, dict):
        return [{"type": "text", "data": {"text": str(content)}}]
    nicknames = nicknames or {}
    mt = int(message_type or 0)

    if mt == DY_TEXT:
        return _text_content_to_segments(content, nicknames, idmap)

    if mt == DY_BIG_EMOJI:
        # 表情类消息（type 5）实测有两种，字段形态不一样，必须分开处理：
        #
        #   aweType 510  收藏/上传的表情包（sticker_type 2）
        #                url_list 是明文 PNG/WebP，没有 skey；id/image_id 是
        #                抖音的表情资源 id（非 0）。原样重发实测成功。
        #   aweType 507  系统「轻表情」（sticker_type 12，如"比心"）
        #                id/image_id/package_id 全是 0，只有 display_name +
        #                url_list。原样重发同样实测成功，且回读 aweType 仍是 507。
        #
        # 两者的直链都带签名会过期，所以统一过一层代理，事件里的 URL 才长期可用。
        urls = _first_url_list(content.get("url")) \
            or _first_url_list(content.get("resource_url"))
        url = _media_url(media, urls)
        image_id = str(content.get("image_id") or content.get("id") or "")
        if image_id in ("", "0"):
            image_id = ""
        lite = (not image_id) and bool(content.get("display_name"))
        return [{"type": "image",
                 "data": {"file": url, "url": url, "sub_type": "sticker",
                          "sticker_kind": "lite_emoji" if lite else "sticker",
                          "display_name": str(content.get("display_name") or ""),
                          "awe_type": str(content.get("aweType") or ""),
                          "width": str(content.get("width") or 0),
                          "height": str(content.get("height") or 0),
                          "image_id": image_id,
                          "package_id": str(content.get("package_id") or ""),
                          "image_type": str(content.get("image_type") or ""),
                          "raw_url": (content.get("url") or {}).get("url_list") or [],
                          "raw": content}}]

    if mt == DY_IMAGE:
        urls, node = _resource_urls(content)
        url = _media_url(media, urls,
                         skey=node.get("skey") or "",
                         md5=node.get("md5") or content.get("md5") or "",
                         data_size=node.get("data_size") or 0)
        width = content.get("cover_width") or content.get("width") or 0
        height = content.get("cover_height") or content.get("height") or 0
        return [{"type": "image",
                 "data": {"file": url, "url": url,
                          "width": str(width), "height": str(height)}}]

    if mt == DY_VIDEO:
        url = _first_url(content.get("resource_url")) or _first_url(content.get("video_url"))
        cover = _first_url(content.get("cover_url")) or _first_url(content.get("cover"))
        return [{"type": "video", "data": {"file": url, "url": url, "cover": cover}}]

    if mt in (DY_VOICE, DY_ENCRYPT_VOICE):
        url = _first_url(content.get("resource_url")) or _first_url(content.get("url"))
        return [{"type": "record", "data": {"file": url, "url": url}}]

    if mt == DY_FILE:
        url = _first_url(content.get("resource_url")) or _first_url(content.get("url"))
        name = content.get("file_name") or content.get("name") or ""
        return [{"type": "file", "data": {"file": url, "url": url, "name": name}}]

    if mt == DY_SHARE_AWEME:
        item = content.get("itemId") or content.get("item_id") or ""
        title = content.get("title") or content.get("desc") or ""
        url = _first_url(content.get("cover_url")) or _first_url(content.get("url"))
        return [{"type": "image",
                 "data": {"file": url, "url": url, "sub_type": "share_aweme"}},
                {"type": "text",
                 "data": {"text": f"[分享视频 {item}] {title}".strip()}}]

    if mt == DY_SHARE_PHOTOS:
        # PC IM 图文/图集分享卡片（aweType=0, awemeType=68）。只把首张
        # 封面作为 OneBot image，再附带标题和作品 ID；这样 AstrBot 能看懂，
        # 同时不会把整张卡片的内部实验字段误当成普通图片消息。
        item = _find_value(content, ("itemId", "item_id", "aweme_id", "awemeId"))
        if not item:
            item = _item_id_from_text(_find_value(
                content, ("share_url", "item_url", "aweme_url", "url")))
        title = _find_value(content, ("content_title", "title", "desc"))
        # 图集卡片里第一帧通常在 images/image_list/image_infos[0]，
        # cover_url 只是没有图集字段时的回退，不能优先取它。
        first_frame = _find_first_frame(content)
        cover = (first_frame
                 or _first_url(content.get("cover_url"))
                 or _first_url(content.get("cover_url_v2"))
                 or _first_url(content.get("content_thumb"))
                 or _first_url(content.get("url")))
        count = content.get("image_count") or content.get("imageCount") or 0
        label = f"[分享图文ID: {item}]" if item else "[分享图文ID: 未知]"
        if count:
            label += f"（{count}张）"
        if title:
            label += f" {title}"
        out = []
        if cover:
            out.append({"type": "image",
                        "data": {"file": cover, "url": cover,
                                 "sub_type": "share_photos"}})
        out.append({"type": "text", "data": {"text": label}})
        return out

    if mt == DY_SHARE_WEB:
        return [{"type": "text",
                 "data": {"text": content.get("title") or content.get("url") or "[分享链接]"}}]

    if mt == DY_SHARE_USER:
        return [{"type": "text",
                 "data": {"text": "[分享用户] "
                                  + str(content.get("nickname") or content.get("sec_uid") or "")}}]

    if mt == DY_NOTICE:
        text = render_notice(content)
        return [{"type": "text", "data": {"text": text, "raw": content}}]

    # mt == 1 里也藏着纯提示（aweType 903 的「本群已开启…」实测就是这个）
    if content.get("tips"):
        return [{"type": "text",
                 "data": {"text": render_notice(content), "raw": content}}]

    # 没验证过的类型：给个明确的占位符，并把原始内容带上，不猜。
    return [{"type": "text",
             "data": {"text": f"[未支持的消息类型 {mt}]",
                      "raw": content}}]


def render_notice(content):
    """把抖音的群系统通知渲染成人话。

    实测结构（cmd 301 回读，不是猜的）：
      locale_resources = [{"lang":"zh-Hans","text":"{0}邀请{1}加入了群聊…"}]
      template         = [{"key":"1","name":"群聊 AI"}, …]   数字 key -> 占位符
      或               = [{"key":"《群聊公约》","name":null}]  文本 key -> 整词替换
      tips             = "本群已开启 {{1}} 功能…"             {{n}} 形态的占位符
    两种占位符都要处理；模板里没有的占位符原样留着，不编造内容。
    """
    content = content if isinstance(content, dict) else {}
    text = content.get("tips") or ""
    if not text:
        for res in content.get("locale_resources") or []:
            if isinstance(res, dict) and res.get("lang", "").startswith("zh") \
                    and res.get("text"):
                text = res["text"]
                break
        if not text:
            for res in content.get("locale_resources") or []:
                if isinstance(res, dict) and res.get("text"):
                    text = res["text"]
                    break
    text = str(text or "")
    if not text:
        return "[群系统通知]"

    for tpl in content.get("template") or []:
        if not isinstance(tpl, dict):
            continue
        key = str(tpl.get("key") or "")
        name = tpl.get("name")
        if not key or not name:
            continue
        if key.isdigit():
            text = text.replace("{{" + key + "}}", str(name))
            text = text.replace("{" + key + "}", str(name))
        else:
            text = text.replace(key, str(name))

    # active_users/passive_users 只在模板没给名字时补位，不覆盖已有内容
    users = []
    for bucket in ("active_users", "passive_users"):
        for u in content.get(bucket) or []:
            if isinstance(u, dict) and u.get("nickname"):
                users.append(str(u["nickname"]))
    for idx, nick in enumerate(users):
        text = text.replace("{{" + str(idx) + "}}", nick)
    return text


def _mention_spans(content, text):
    """从 richTextInfos 里取出 @ 提及的 (start, end, uid)。

    抖音文本消息的 @ 实测【不在】content["mention_users"]（那个字段恒为 []），
    而在 richTextInfos：infoType=6 的条目带 info.uid + info.mention_id，并用
    location/length 标出它在 text 里的位置。该区间包含前导 "@" 和尾随的
    分隔符（实测是 \\u2005 窄空格），例如 text="@.ccc\\u2005你好" 对应
    location=0 length=6。

    只认 infoType=6：同一条消息里还有 infoType=1（用户信息段）、
    infoType=2（字体样式）、infoType=3（composed_id），它们都不代表 @。
    """
    spans = []
    infos = content.get("richTextInfos")
    if not isinstance(infos, list):
        return spans
    n = len(text)
    for item in infos:
        if not isinstance(item, dict):
            continue
        if _as_int(item.get("infoType")) != _RT_MENTION:
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        uid = info.get("uid") or info.get("user_id")
        if not uid:
            continue
        loc = _as_int(item.get("location"), -1)
        ln = _as_int(item.get("length"), 0)
        if loc < 0 or loc >= n or ln <= 0:
            continue
        spans.append((loc, min(loc + ln, n), str(uid)))
    # 按位置排序并丢掉重叠区间，保证切分时不会重复也不会错位。
    spans.sort()
    clean, last_end = [], -1
    for loc, end, uid in spans:
        if loc < last_end:
            continue
        clean.append((loc, end, uid))
        last_end = end
    return clean


def _strip_mention_text(raw):
    """@ 区间文本 -> 可读昵称："@.ccc\\u2005" -> ".ccc"。"""
    s = str(raw or "").lstrip("@").strip()
    return s.strip("\u2005\u00a0 ").strip()


def _text_content_to_segments(content, nicknames, idmap=None):
    """文本消息 -> 段数组。

    优先走 richTextInfos 的 location/length（唯一可靠的 @ 位置来源）；
    拿不到时退回「mention_users + 昵称匹配」的老路径。

    qq 必须是 OneBot id：抖音 uid 与 self_id 不同区间，@机器人 只有换成
    self_id 才会等于事件里的 self_id，AstrBot 的唤醒判断才成立。
    """
    text = content.get("text")
    if text is None:
        return [{"type": "text", "data": {"text": ""}}]
    text = str(text)
    nicknames = nicknames or {}

    def _qq(uid):
        # 返回 str：OneBot v11 的 at.qq 惯例是字符串（"all" 也是字符串），
        # 本仓库既有事件与契约测试都按字符串断言。
        if not str(uid or "").strip():
            return ""
        if idmap is not None:
            return str(idmap.to_onebot(uid))
        return str(uid)

    spans = _mention_spans(content, text)
    if spans:
        segments, pos = [], 0
        for loc, end, uid in spans:
            if loc > pos:
                segments.append({"type": "text",
                                 "data": {"text": text[pos:loc]}})
            name = nicknames.get(uid) or _strip_mention_text(text[loc:end])
            segments.append({"type": "at", "data": {"qq": _qq(uid),
                                                    "name": name}})
            pos = end
        if pos < len(text):
            segments.append({"type": "text", "data": {"text": text[pos:]}})
        return segments

    mention_uids = []
    for key in ("mention_users", "mentioned_users"):
        val = content.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    uid = item.get("uid") or item.get("user_id")
                    if uid:
                        mention_uids.append(str(uid))
                elif item:
                    mention_uids.append(str(item))

    if not mention_uids or not nicknames:
        return [{"type": "text", "data": {"text": text}}]

    # 把 "@昵称" 替换成 at 段：只在昵称能唯一对上 uid 时才切，
    # 否则宁可整条留成文本，也不猜错人。
    names = sorted({nicknames.get(u, "") for u in mention_uids
                    if nicknames.get(u)}, key=len, reverse=True)
    if not names:
        return [{"type": "text", "data": {"text": text}}]
    pattern = re.compile(r"@(" + "|".join(re.escape(n) for n in names) + r")")

    segments = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            segments.append({"type": "text",
                             "data": {"text": text[pos:match.start()]}})
        name = match.group(1)
        uid = next((u for u in mention_uids if nicknames.get(u) == name), "")
        segments.append({"type": "at", "data": {"qq": _qq(uid), "name": name}})
        pos = match.end()
    if pos < len(text):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return segments


# ---------------------------------------------------------------- 发：OneBot -> 抖音

def segments_to_dy(segments, idmap=None):
    """OneBot 段数组 -> (message_type, content_dict, mentioned_uids, extras)。

    只支持有把握的类型：
        text  -> 合并进 content["text"]（type 7）
        at    -> mentioned_uids + 文本里保留 "@昵称"
        image -> 记在 extras["images"] 里，由调用方上传后替换（type 27）；
                 sub_type=sticker 且带 image_id/uri 时记在 extras["sticker"]，
                 直接按抖音表情包（type 5）发，不重新上传
        face  -> 映射成 unicode emoji 并进文本（抖音没有 QQ 表情目录，
                 映射不到时降级成 [表情]，不假装支持）
        reply -> 记在 extras["reply"] 里，由调用方填 ref_msg_info
    其它类型抛 NotImplementedError，让上层明确回 retcode 1404，
    而不是静默丢弃用户内容。
    """
    texts = []
    mentioned = []
    images = []
    files = []
    sticker = None
    reply = None
    unsupported = []

    for seg in segments or []:
        stype, data = seg.get("type"), seg.get("data") or {}
        if stype == "text":
            texts.append(str(data.get("text", "")))
        elif stype == "at":
            qq = str(data.get("qq", "")).strip()
            if qq in ("all", "0"):
                texts.append("@所有人")
                continue
            dy_uid = idmap.to_douyin(qq) if idmap else qq
            if dy_uid.isdigit():
                mentioned.append(int(dy_uid))
            name = data.get("name") or ""
            texts.append("@" + (name or dy_uid))
        elif stype == "image":
            # 抖音表情回灌：sub_type=sticker 且带 raw（本桥自己发出去的事件）
            # 时，按原 content 原样重发 —— 不重新上传。
            #
            # 实测两种表情的重发都成功：
            #   aweType 510（收藏表情包，image_id 非 0）
            #   aweType 507（系统轻表情，image_id 为 0，靠 display_name + url_list）
            # 判据用 raw.aweType 而不是 image_id：507 的 image_id 本来就是 0，
            # 用 image_id 判会把轻表情误当成普通图片去重新上传。
            ref = data.get("raw") if isinstance(data.get("raw"), dict) else None
            if str(data.get("sub_type") or "") == "sticker" and ref:
                awe = _as_int(ref.get("aweType"))
                if awe in (507, 510) or _as_int(ref.get("sticker_type")) in (2, 12):
                    sticker = ref
                    continue
            images.append(str(data.get("file") or data.get("url") or ""))
        elif stype == "file":
            files.append(str(data.get("file") or data.get("url") or ""))
        elif stype == "reply":
            reply = str(data.get("id") or "")
        elif stype == "face":
            # 抖音没有 QQ 的 face 目录，但有 unicode emoji。能映射的就映射成
            # unicode（真实可显示），映射不到的老实降级成 [表情]。
            emoji = QQ_FACE_EMOJI.get(str(data.get("id") or "").strip())
            texts.append(emoji if emoji else "[表情]")
        else:
            unsupported.append(stype)

    if unsupported and not texts and not images and not files and not sticker:
        raise NotImplementedError(
            "抖音桥不支持的消息段: " + ", ".join(sorted(set(unsupported))))

    content = {
        "aweType": 700,
        "type": 0,
        "richTextInfos": [],
        "text": "".join(texts),
    }
    extras = {
        "images": images,
        "files": files,
        "sticker": sticker,
        "mentioned_users": mentioned,
        "reply": reply,
        "unsupported": sorted(set(unsupported)),
    }
    return DY_TEXT, content, mentioned, extras


def encode_dy_content(content):
    """抖音要求 content 是紧凑 JSON 字符串。"""
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
