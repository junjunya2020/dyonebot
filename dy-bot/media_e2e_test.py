# -*- coding: utf-8 -*-
"""图片链路端到端自测：真实推送帧 -> 解密服务 -> OneBot 事件段。

覆盖 4 件事（全部走真实代码路径，不 mock 业务逻辑）：
  1. 用 Im_pb2/Live_pb2 造一条**真实的**抖音图片推送帧，走 parse_push；
  2. 走 EventBuilder.message_event 得到 OneBot 事件，断言 image 段是
     本桥的 /media/<token> URL（而不是会显示成密文的 CDN 直链）；
  3. 真的 HTTP GET 那个 URL，断言拿回来的是**真 PNG**，md5 == 抖音 md5；
  4. 反向：把事件里的图片段原样送回去，断言走的是 upload_image（type 27）。
"""
import hashlib
import json
import sys
import threading
import urllib.request

sys.path.insert(0, "/root/dyonebot/dy-bot")
sys.path.insert(0, "/root/dyonebot/douyin-spider")
sys.path.insert(0, "/root/dyonebot/dy-bot/proto")

from onebot_bridge import cq                                    # noqa: E402
from onebot_bridge.bridge import Bridge, EventBuilder            # noqa: E402
from onebot_bridge.dyim import DouyinIM, parse_push               # noqa: E402
from onebot_bridge.ids import IdMap                               # noqa: E402
from onebot_bridge.media import MediaCache                        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _start_server(media):
    """在后台线程里真的起一个 aiohttp，把 media.routes 挂上去。

    只测 ensure() 不能证明「HTTP 这一腿」通：事件里给客户端的是 URL，
    真正的解密发生在有人 GET 的那一刻。所以这里起真服务、发真请求。
    """
    import asyncio
    from aiohttp import web

    async def run():
        app = web.Application()
        media.routes(app, web)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", media.port).start()
        await asyncio.Future()

    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(run())),
                     daemon=True).start()
    import time
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{media.public_base}/", timeout=1)
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.1)
    return True


def main():
    cfg = json.load(open("/root/dyonebot/dy-bot/onebot_bridge.config.json"))
    mconf = dict(cfg.get("media") or {})
    mconf.update({"dir": "/tmp/media_e2e_cache", "port": 6899,
                  "public_host": "127.0.0.1", "public_base": ""})
    media = MediaCache(mconf)
    _start_server(media)

    im = DouyinIM()
    idmap = IdMap("/tmp/media_e2e_state.json")
    bridge = Bridge(cfg, im, idmap, 7000669513203620634, media=media)

    print("\n[1] 真实推送帧 -> parse_push")
    sample = json.load(open("/tmp/imgmsg.json"))
    from Im_pb2 import ImMessage, Response as ImResponse      # noqa: E402
    from static import Live_pb2                                # noqa: E402
    PushFrame = Live_pb2.PushFrame
    msg = ImMessage()
    msg.conversation_id = sample["conversation_id"]
    msg.conversation_type = int(sample["conversation_type"])
    msg.conversation_short_id = int(sample["short_id"])
    msg.message_type = int(sample["message_type"])
    msg.sender = 2916383924691644  # 非 self：skip_self 会正确过滤自己的消息
    msg.sender_sec_uid = sample["sender_sec_uid"]
    msg.server_message_id = int(sample["server_message_id"])
    msg.content = json.dumps(sample["content"], ensure_ascii=False)
    resp = ImResponse()
    resp.cmd = 100
    resp.body.new_message_notify.message.CopyFrom(msg)
    frame = PushFrame()
    frame.payloadType = "pb"
    frame.payload = resp.SerializeToString()
    raw = frame.SerializeToString()

    kind, payload = parse_push(raw)
    check("推送帧解析成 message", kind == "message", f"kind={kind}")
    check("message_type=27 保留", int(payload["message_type"]) == 27)
    check("skey 从 content 里读到了",
          payload["content"]["resource_url"]["skey"].startswith("7cf56de9"))

    print("\n[2] 事件构造：图片段必须是本桥 /media/ 代理 URL")
    ev = bridge.builder.message_event(payload)
    check("事件构造成功", bool(ev))
    segs = [s for s in ev["message"] if s["type"] == "image"]
    check("有 image 段", len(segs) == 1, f"n={len(segs)}")
    url = segs[0]["data"]["url"] if segs else ""
    check("URL 指向本桥图片服务（不是 CDN 直链）",
          "/media/" in url and "douyinpic.com" not in url, url)
    check("raw CQ 码里也是代理 URL（客户端照 raw 发也不会拿到密文）",
          "/media/" in ev["raw_message"] and "douyinpic.com" not in ev["raw_message"],
          ev["raw_message"][:90])
    check("宽高从 cover_* 取到", segs and segs[0]["data"]["width"] == "64",
          segs[0]["data"]["width"] if segs else "")

    print("\n[3] 真取一次：HTTP GET -> 真 PNG，md5 与抖音一致")
    token = url.rsplit("/", 1)[-1] if url else ""
    check("token 就是抖音给的 md5",
          token == sample["content"]["resource_url"]["md5"], token)
    body = urllib.request.urlopen(url, timeout=30).read()
    check("HTTP 取回来的不是密文（有 PNG magic）",
          body.startswith(b"\x89PNG\r\n\x1a\n"), body[:8].hex())
    check("md5 与 content.md5 完全一致",
          hashlib.md5(body).hexdigest() == sample["content"]["md5"],
          hashlib.md5(body).hexdigest())
    check("长度与 data_size 一致",
          len(body) == int(sample["content"]["resource_url"]["data_size"]),
          f"{len(body)} vs {sample['content']['resource_url']['data_size']}")

    print("\n[4] 反向：事件里的图片段送回去 -> 上传成 type 27")
    mt, content, mentioned, extras = bridge.actions.build_dy_payload(ev["message"])
    check("送回的图片段被识别为 image（type 27）", mt == 27, f"mt={mt}")
    check("上传结果带 resource_url", "resource_url" in content)

    print("\n[5] 表情包（type 5）：收藏表情 + 系统轻表情，收 -> 发 全程不重新上传")
    def _sticker_msg(content, mid):
        return {
            "conversation_id": sample["conversation_id"],
            "conversation_type": 2, "message_type": 5,
            "sender": "2916383924691644", "sender_sec_uid": "",
            "server_message_id": mid, "short_id": 0, "index": 0,
            "content": content, "ext": {}, "create_time": 1790353241010,
        }

    rich = {"aweType": 510, "display_name": "", "sticker_type": 2,
            "image_id": 7673810053870125105, "image_type": "webp",
            "width": 783, "height": 783, "package_id": -2916383924691644,
            "resource_type": 0,
            "url": {"uri": "tos-cn-o-0812/abc",
                    "url_list": ["https://p5-ex-gddgtc-sign.douyinpic.com/obj/x"]}}
    sev = bridge.builder.message_event(_sticker_msg(rich, 111))
    sseg = [s for s in sev["message"] if s["type"] == "image"][0]
    check("收藏表情有 sub_type=sticker", sseg["data"]["sub_type"] == "sticker")
    check("收藏表情 image_id 保留", sseg["data"]["image_id"] == "7673810053870125105")
    check("收藏表情 kind=sticker", sseg["data"]["sticker_kind"] == "sticker")
    smt, sc, _m, _e = bridge.actions.build_dy_payload([sseg])
    check("收藏表情原样重发为 type 5", smt == 5, f"mt={smt}")
    check("aweType=510 保留", sc.get("aweType") == 510)
    check("image_id/uri 原样保留",
          str(sc.get("image_id")) == "7673810053870125105"
          and sc["url"]["uri"] == "tos-cn-o-0812/abc")
    check("width/height/package_id 保留",
          sc["width"] == 783 and sc["package_id"] == -2916383924691644)

    # 系统轻表情（aweType 507）：image_id/package_id 全是 0，靠 display_name + url_list
    lite = {"aweType": 507, "display_name": "比心", "sticker_type": 12,
            "id": 0, "image_id": 0, "image_type": "png", "package_id": 0,
            "resource_type": 4, "width": 100, "height": 100,
            "url": {"uri": "", "url_list": ["https://p26-sign.douyinpic.com/obj/im/emoji.png"]}}
    lev = bridge.builder.message_event(_sticker_msg(lite, 112))
    lseg = [s for s in lev["message"] if s["type"] == "image"][0]
    check("轻表情能识别出来（kind=lite_emoji）",
          lseg["data"]["sticker_kind"] == "lite_emoji", lseg["data"]["sticker_kind"])
    check("轻表情 display_name 保留", lseg["data"]["display_name"] == "比心")
    lmt, lc, _m, _e = bridge.actions.build_dy_payload([lseg])
    check("轻表情也原样重发为 type 5（不是当普通图重新上传）", lmt == 5, f"mt={lmt}")
    check("轻表情 aweType=507 保留（image_id 本就是 0）", lc.get("aweType") == 507)
    check("轻表情 display_name 保留", lc.get("display_name") == "比心")

    print("\n[6] QQ face 段 -> unicode emoji（映射不到要诚实降级）")
    cmt, cc, _m, _e = bridge.actions.build_dy_payload(
        [{"type": "face", "data": {"id": "1"}},
         {"type": "face", "data": {"id": "999999"}},
         {"type": "text", "data": {"text": " ok"}}])
    check("能映射的 face -> unicode emoji", "😖" in cc["text"], repr(cc["text"]))
    check("映射不到的 face -> [表情]（不伪造）", "[表情]" in cc["text"], repr(cc["text"]))
    check("face 走文本 type 7", cmt == 7)

    print("\n[7] 没有 skey 的明文直链不会被误伤")
    plain = MediaCache({"dir": "/tmp/mcache_plain", "public_host": "127.0.0.1",
                        "port": 6801, "enabled": False})
    segs2 = cq.dy_content_to_segments(27, sample["content"], None, media=plain)
    check("media 关闭时退回原始直链（不把消息变空）",
          "douyinpic.com" in segs2[0]["data"]["url"], segs2[0]["data"]["url"][:60])

    print("\n" + "=" * 58)
    print(f"结果: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
