# -*- coding: utf-8 -*-
"""群聊发送链路（先验 ticket，再发）。

用法:
  .venv/bin/python -u send_group.py ticket          # 只查票
  .venv/bin/python -u send_group.py send "内容"      # 发群消息
"""
import json
import os
import sys

sys.path.insert(0, "/root/dyonebot/douyin-spider")
os.chdir("/root/dyonebot/douyin-spider")

import requests

from dy_apis.douyin_api import DouyinAPI, protobuf_to_dict
from builder.header import HeaderBuilder, HeaderType
from builder.proto import ProtoBuilder
from utils.common_util import load_env
from static import Request_pb2, Response_pb2

requests.packages.urllib3.disable_warnings()

GROUP_ID = "7689446943537234469"
GROUP_TYPE = 2


def fresh_ticket(auth, conv_id, short_id, conv_type=GROUP_TYPE):
    """用 get_info_list 换群会话的票。"""
    url = "https://imapi.douyin.com/v2/conversation/get_info_list"
    req = ProtoBuilder.build_normal_request(auth, 610)
    body = req.body.get_conversation_info_list_v2_body.data
    body.conversation_id = str(conv_id)
    body.conversation_short_id = int(short_id)
    body.conversation_type = int(conv_type)

    headers = HeaderBuilder().build(HeaderType.PROTOBUF)
    headers.set_header('referer', 'https://www.douyin.com/')
    headers.with_bd("/v2/conversation/get_info_list", auth)

    resp = requests.post(url, headers=headers.get(), cookies=auth.cookie,
                         data=req.SerializeToString(), verify=False, timeout=20)
    print("HTTP", resp.status_code, "len", len(resp.content), flush=True)
    rp = Response_pb2.Response()
    rp.ParseFromString(resp.content)
    print("cmd", rp.cmd, "msg", rp.message, "err", rp.error_desc, flush=True)

    # 结构化解析
    try:
        import blackboxprotobuf
        data, _mt = blackboxprotobuf.decode_message(resp.content)
        conv = data['6']['610']['1']

        def text(node):
            return node.decode('utf-8') if isinstance(node, bytes) else node

        core = conv.get('50') or {}
        return {
            "conversation_id": text(conv.get('1')),
            "short_id": text(conv.get('2')),
            "type": text(conv.get('3')),
            "ticket": text(conv.get('4')),
            "name": text(core.get('5')),
        }
    except Exception as err:
        print("parse err", type(err).__name__, err, flush=True)
        return None


def send_group(auth, text, ticket, short_id):
    """发群消息：conversation_type=2，conversation_id 用纯数字群号。"""
    url = 'https://imapi.douyin.com/v1/message/send'
    headers = HeaderBuilder().build(HeaderType.PROTOBUF)
    headers.set_header('referer', 'https://www.douyin.com/')
    headers.with_bd('/v1/message/send', auth)
    identity_token, identity_device_id = DouyinAPI.get_identity_security_token(auth)
    payload = {"aweType": 700, "type": 0, "richTextInfos": [], "text": text}
    req = ProtoBuilder.build_send_message_request(
        auth, GROUP_ID, int(short_id), ticket,
        content=payload, message_type=DouyinAPI.IM_TEXT,
        identity_security_token=identity_token,
        identity_security_device_id=identity_device_id,
    )
    # 群聊：改类型
    req.body.send_message_body.conversation_type = GROUP_TYPE
    # 浏览器群发消息带的 ext
    req.body.send_message_body.ext.append(
        Request_pb2.ExtValue(key='chat_scene', value='group'))
    req.body.send_message_body.ext.append(
        Request_pb2.ExtValue(key='s:chat_tag', value='public_group'))

    from utils.dy_util import generate_a_bogus, splice_url
    params = {'msToken': auth.msToken}
    params['a_bogus'] = generate_a_bogus(splice_url(params), host='www.douyin.com')
    params['verifyFp'] = auth.cookie['s_v_web_id']
    params['fp'] = auth.cookie['s_v_web_id']

    resp = requests.post(url, params=params, headers=headers.get(), verify=False,
                         cookies=auth.cookie, data=req.SerializeToString(), timeout=25)
    rp = Response_pb2.Response()
    rp.ParseFromString(resp.content)
    print("SEND https:", resp.status_code, "cmd", rp.cmd,
          "msg", rp.message, "err", rp.error_desc, flush=True)
    return rp.message == 'OK'


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ticket"
    auth = load_env()
    print("my_uid", auth.get_uid(), flush=True)

    info = fresh_ticket(auth, GROUP_ID, GROUP_ID)
    print("INFO:", json.dumps(info, ensure_ascii=False), flush=True)
    if not info or not info.get("ticket"):
        print("FAIL 没拿到群票", flush=True)
        return 1
    if mode == "ticket":
        return 0

    text = sys.argv[2] if len(sys.argv) > 2 else "测试"
    ok = send_group(auth, text, info["ticket"], info["short_id"])
    print("RESULT", ok, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
