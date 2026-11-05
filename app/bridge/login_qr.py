#!/usr/bin/env python3
"""扫码登录：米家 App 扫二维码 → 拿到 passToken → 生成 mistervice 用的 .mi.token

用法：
    python login_qr.py [--config config.json]

流程：
    1. 向小米申请登录二维码，终端打印 ASCII 码，同时输出可打开的二维码图片链接
    2. 用米家 App 扫码确认（120 秒内）
    3. 自动换取 micoapi 的 serviceToken，写入 conf/.mi.token
"""
import argparse
import asyncio
import json
import os
import shutil

from aiohttp import ClientSession
from miservice import MiAccount

from bridge.config import Config
from bridge.mijia_qr import MiJiaAPI


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = Config(args.config)
    conf_dir = os.path.dirname(cfg.abspath(cfg.mi.get("token_store")))
    token_path = cfg.abspath(cfg.mi.get("token_store"))
    os.makedirs(conf_dir, exist_ok=True)

    print("=" * 50)
    print("即将显示登录二维码，请用【米家 App】扫描")
    print("=" * 50)

    api = MiJiaAPI(auth_data_path=conf_dir)
    data = api.qr_login()
    print("小米侧认证完成，passToken 已获取")

    # 用 passToken 换 micoapi 的 serviceToken
    async with ClientSession() as sess:
        acc = MiAccount(sess, "", "", token_path)
        acc.token = {
            "passToken": data["passToken"],
            "userId": data["userId"],
            "deviceId": data["deviceId"],
        }
        ok = await acc.login("micoapi")
        print("生成 .mi.token ->", ok)
        if not ok:
            raise SystemExit("换取 serviceToken 失败")
        print("token keys:", list(acc.token.keys()))

    if os.path.isfile(token_path):
        shutil.copy2(token_path, token_path + ".bak")
    print("完成：", token_path)


if __name__ == "__main__":
    asyncio.run(main())
