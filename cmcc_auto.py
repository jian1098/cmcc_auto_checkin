# -*- coding: utf-8 -*-
"""中国移动（wx.10086.cn 签到有礼）自动签到 + 飞书推送。

用法：
  - GitHub Actions 定时运行（见 .github/workflows/daily.yml）
  - Secrets：
      CMCC_COOKIE    必填，签到接口的 Cookie；多账号用换行或空行分隔
      FEISHU_WEBHOOK 可选，飞书自定义机器人 Webhook，配置后推送结果卡片

本地调试：
  set CMCC_COOKIE=... && python cmcc_auto.py
  （PowerShell：$env:CMCC_COOKIE="..."; python cmcc_auto.py）

只依赖 Python 标准库，无第三方包。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# ----------------------------- 配置 -----------------------------

SIGN_URL = "https://wx.10086.cn/qwhdhub/api/mark/do/mark"

MAX_RETRY = 3          # 网络异常 / 5xx 重试次数
RETRY_BACKOFF = 2.0    # 退避基数（秒），第 n 次重试退避 n*RETRY_BACKOFF
REQUEST_TIMEOUT = 15

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()
RESULT_FILE = os.environ.get("RESULT_FILE", "").strip()

# ----------------------------- 基础工具 -----------------------------


def log(msg):
    print(msg, flush=True)


def masked(cookie):
    """日志里脱敏展示 Cookie。"""
    if not cookie:
        return "(空)"
    if len(cookie) <= 12:
        return cookie[:4] + "****"
    return cookie[:8] + "****" + cookie[-4:]


def post_json(url, headers, payload=None):
    """POST 请求，带重试。返回 (http_status, body_dict_or_text)。"""
    data = json.dumps(payload or {}).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                        context=ssl.create_default_context()) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  HTTP %s，退避后重试（%d/%d）" % (e.code, attempt, MAX_RETRY))
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return e.code, ""
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  请求异常，退避后重试（%d/%d）：%s: %s"
                    % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return -1, "%s: %s" % (type(e).__name__, e)


def parse_body(text):
    try:
        return json.loads(text)
    except Exception:
        return None

# ----------------------------- 签到 -----------------------------


def sign_headers(cookie):
    return {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36 "
                       "MicroMessenger/8.0.47"),
        "Referer": "https://wx.10086.cn/",
        "Origin": "https://wx.10086.cn",
    }


def do_sign(cookie, index):
    """执行一次签到，返回 (ok, 摘要文案)。"""
    status, text = post_json(SIGN_URL, sign_headers(cookie))
    if status != 200:
        return False, "HTTP %s（Cookie 可能失效或网络异常）" % status
    body = parse_body(text)
    if body is None:
        return False, "响应不是合法 JSON：%s" % text[:120]

    code = body.get("code", "")
    success = body.get("success", False)
    if success:
        data = body.get("data") or {}
        prize = data.get("prizeName") or "未返回奖品"
        cat = data.get("categoryOverview") or {}
        month_total = cat.get("valueDesc") or (str(cat.get("totalValue", "?"))
                                               + str(cat.get("unit") or ""))
        return True, "签到成功，获得「%s」，本月累计 %s" % (prize, month_total)
    if code in ("ALL_MARKED", "TODAY_MARKED"):
        return True, "今日已签到，名额已全部领完（幂等跳过）" if code == "ALL_MARKED" \
            else "今日已签到（重复触发，幂等跳过）"
    return False, "签到失败：code=%s，msg=%s" % (code or "无", body.get("msg") or text[:120])

# ----------------------------- 飞书推送 -----------------------------


def push_feishu(webhook, sections, overall_ok):
    """推送到飞书自定义机器人。返回 (ok, reason)。"""
    color = "green" if overall_ok else "red"
    lines = []
    for name, line in sections:
        if lines:
            lines.append("")
        if name:
            lines += ["**%s**" % name, line]
        else:
            lines += [line]
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {"tag": "plain_text",
                          "content": "中国移动每日签到 · %s"
                                     % ("成功" if overall_ok else "有失败项")},
            },
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        attempt += 1
        try:
            req = urllib.request.Request(webhook, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                        context=ssl.create_default_context()) as resp:
                body = parse_body(resp.read().decode("utf-8", "replace")) or {}
                log("  [飞书] 响应：%s" % json.dumps(body, ensure_ascii=False))
                if body.get("code") == 0:
                    return True, ""
                return False, ("飞书返回码 %s（19001 多为地址不对；机器人若开启签名校验也会失败，"
                               "建机器人时不要勾选）" % body.get("code", "未知"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < MAX_RETRY:
                log("  [飞书] HTTP %s，退避后重试（%d/%d）" % (e.code, attempt, MAX_RETRY))
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return False, "HTTP %s" % e.code
        except Exception as e:
            if attempt < MAX_RETRY:
                log("  [飞书] 请求异常，退避后重试（%d/%d）：%s: %s"
                    % (attempt, MAX_RETRY, type(e).__name__, e))
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)

# ----------------------------- 主流程 -----------------------------


def load_cookies(raw):
    """多账号：Cookie 之间用换行分隔（空行忽略）。"""
    return [c.strip() for c in raw.replace("\r", "\n").split("\n") if c.strip()]


def main():
    raw = os.environ.get("CMCC_COOKIE", "")
    cookies = load_cookies(raw)
    if not cookies:
        log("未配置 CMCC_COOKIE（Secrets），退出。")
        return 1

    sections = []
    overall_ok = True
    single = len(cookies) == 1
    for i, cookie in enumerate(cookies):
        name = "" if single else "账号 %d（%s）" % (i + 1, masked(cookie))
        log("[%s] 开始签到…" % ("单账号" if single else name))
        ok, summary = do_sign(cookie, i)
        log("[%s] %s" % ("单账号" if single else name, summary))
        sections.append((name, summary))
        if not ok:
            overall_ok = False

    # 汇总写入结果文件（供 Actions step summary 使用）
    if RESULT_FILE:
        try:
            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                for name, line in sections:
                    f.write(("- %s：%s\n" % (name, line)) if name else "- %s\n" % line)
        except OSError as e:
            log("写入结果文件失败：%s" % e)

    if FEISHU_WEBHOOK:
        ok, reason = push_feishu(FEISHU_WEBHOOK, sections, overall_ok)
        log("[飞书] %s" % ("推送成功" if ok else "推送失败：%s" % reason))
    else:
        log("未配置 FEISHU_WEBHOOK，跳过飞书推送。")

    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(main())
