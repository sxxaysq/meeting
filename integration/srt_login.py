#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""若水数据中台登录助手：验证码获取 → OCR → 登录 → 输出 token。"""
import base64
import json
import re
import sys
import urllib.request

GATEWAY = "http://127.0.0.1:8082"


def _post(url, payload, token=None, retries=1):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"), headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_captcha():
    data = _get(GATEWAY + "/sys/auth/captcha")
    return data  # 期望 {key, image}


def ocr_image(img_b64):
    import ddddocr
    raw = base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", img_b64))
    ocr = ddddocr.DdddOcr(show_ad=False)
    return ocr.classification(raw)


def preprocess(raw):
    """像素风验证码带彩色噪点：放大 + 亮度二值化去除彩色噪点。"""
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
        px = img.load()
        for x in range(img.width):
            for y in range(img.height):
                r, g, b = px[x, y]
                # 噪点是彩色小点（饱和度较高），数字是深色系；背景为浅灰/白
                if r > 150 and g > 150 and b > 150:
                    px[x, y] = (255, 255, 255)
                else:
                    # 彩色噪点：三通道差异大且整体偏亮 → 置白；深色像素点保留为黑
                    mx, mn = max(r, g, b), min(r, g, b)
                    if mx > 120 and (mx - mn) > 60:
                        px[x, y] = (255, 255, 255)
                    else:
                        px[x, y] = (0, 0, 0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return raw


def login(max_tries=5):
    import ddddocr  # noqa - 确保已安装
    ocr = ddddocr.DdddOcr(show_ad=False)
    for attempt in range(max_tries):
        cap = get_captcha()
        key = cap.get("key") or cap.get("data", {}).get("key")
        img = cap.get("image") or cap.get("data", {}).get("image")
        if not img:
            print("captcha 响应结构异常:", json.dumps(cap, ensure_ascii=False)[:300])
            return None
        raw = base64.b64decode(re.sub(r"^data:image/\w+;base64,", "", img))
        code = ocr.classification(preprocess(raw))
        if not code:
            code = ocr.classification(raw)
        payload = {
            "key": key,
            "captcha": code,
            "username": "admin",
            "password": "YOUR_PASSWORD",
        }
        try:
            resp = _post(GATEWAY + "/sys/auth/login", payload)
            print("登录响应:", json.dumps(resp, ensure_ascii=False)[:400])
            data = resp.get("data") or {}
            token = data.get("access_token") or data.get("token")
            if resp.get("code") in (0, 200) and token:
                return token
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print("HTTP", e.code, body[:200])
    return None


if __name__ == "__main__":
    token = login()
    if token:
        print("TOKEN=" + token)
    else:
        print("登录失败", file=sys.stderr)
        sys.exit(1)
