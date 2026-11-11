# -*- coding: utf-8 -*-
"""中国移动 App「签到领流量/话费」自动签到 + 飞书推送。

接口链路（wx.10086.cn / qwhdhub mark31 活动，移植自 cmcc-auto-checkin 项目）：

  1. GET  /qwhdsso/login?actUrl=<活动页>        取得内嵌一次性 sid 的登录中转页
  2. POST /qwhdsso/appTokenLogin?sid=...        用 App 票据（或缓存 jwt）换取活动页跳转 URL
  3. GET  <活动页?token=QWHDSSOD...>            服务器 Set-Cookie: QWHD_SESSION_TOKEN
  4. POST /qwhdhub/api/mark/mark31/markstatus   查询签到状态（幂等保护）
  5. POST /qwhdhub/api/mark/mark31/domark       执行签到 {"date":"YYYYMMDD"}
  6. POST /qwhdhub/api/mark/mark31/taskAward/<id>  领取累计/连签奖励

凭证来源（优先级：config.json > 环境变量）：
  CMCC_ACCOUNTS    JSON：抓包 appTokenLogin 请求体原文（单个对象或数组），例如
                     {"provinceCode":"200","cityCode":"0759","appVersionCode":"12.5.7",
                      "userCheckId":"44385a489","carrierOperator":"002","jwtToken":null,
                      "token":"JSESSIONID=...; UID=...; ticketID=SuZhou; Secure","took":251}
                   脚本自动解析：token→app_token、userCheckId(十六进制)→手机号、
                   provinceCode/cityCode/carrierOperator/appVersionCode；
                   jwtToken/took 忽略。多账号用数组或 {"accounts":[...]} 包裹。
                   也兼容归一化字段名（app_token/phone/province_code/...）。
  或单账号用独立环境变量：
  CMCC_APP_TOKEN / CMCC_PHONE / CMCC_PROVINCE_CODE / CMCC_CITY_CODE /
  CMCC_ACTIVITY_ID / CMCC_CHANNEL_ID
  本地调试也可用 --config config.json（参考 config.example.json，已 gitignore）

通知：
  FEISHU_WEBHOOK            飞书自定义机器人 Webhook（可选，推送结果卡片）
  RESULT_FILE               汇总文案写入路径（供 GitHub Actions step summary）

用法：
  python cmcc_auto.py                 # 正式签到（自动领累计/连签奖励）
  python cmcc_auto.py --dry-run       # 只查状态并报「可领未领」奖励
  python cmcc_auto.py --no-claim      # 只签到，不领奖
  python cmcc_auto.py --delay 600     # 启动前随机延迟 0~600 秒（防风控）

退出码：0 = 全部成功或今日已签；2 = 有账号失败；1 = 配置/异常。
需要 requests：pip install -r requirements.txt
"""

import argparse
import ipaddress
import json
import logging
import os
import random
import re
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

# ---------------------------------------------------------------- 常量

BASE = "https://wx.10086.cn"
SSO_LOGIN = BASE + "/qwhdsso/login"
API_MARK = BASE + "/qwhdhub/api/mark"

# 与抓包完全一致的 App WebView UA（服务端校验 leadeon 标识）
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/wkwebview "
    "leadeon/12.5.2/CMCCIT"
)

# 常见内网/元数据主机名（不依赖 DNS 解析即可识别）
LOCAL_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost",
    "metadata.google.internal", "instance-data",
}

REQUEST_TIMEOUT = 30
MAX_RETRY = 3
RETRY_BACKOFF = 2.0

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()
RESULT_FILE = os.environ.get("RESULT_FILE", "").strip()

log = logging.getLogger("cmcc_auto")


class LegacyTLSAdapter(HTTPAdapter):
    """wx.10086.cn 前端网关只接受老式 TLS 套件（如 ECDHE-RSA-AES128-SHA），
    Python 默认的现代 GCM 套件会在握手阶段被拒（handshake failure）。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def assert_safe_url(url: str, allowed_hosts: set | None = None):
    """动态 URL 出网前的 SSRF 边界校验：仅允许 https、公网 IP、域名白名单。"""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"仅允许 https 协议: {url!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"URL 缺少主机名: {url!r}")
    if host in LOCAL_HOSTNAMES:
        raise ValueError(f"拒绝访问内网主机名: {host}")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"主机 {host!r} 不在白名单 {sorted(allowed_hosts)} 内")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名形式，交由 https 证书校验兜底
    if not ip.is_global:
        raise ValueError(f"拒绝访问非公网地址: {ip}")


# ---------------------------------------------------------------- 配置

# 抓包 appTokenLogin 请求体字段 -> 内部字段
_CAPTURE_KEY_MAP = {
    "province_code": "provinceCode",
    "city_code": "cityCode",
    "carrier_operator": "carrierOperator",
    "app_version_code": "appVersionCode",
}


def _normalize_capture(raw: dict) -> dict:
    """把抓包 appTokenLogin 请求体 JSON 归一化为内部字段名。

    抓包字段：token（App 票据）、userCheckId（手机号十六进制）、
    provinceCode/cityCode/carrierOperator/appVersionCode（驼峰）。
    jwtToken / took 字段忽略（jwt 由脚本自行缓存续期，took 每次随机）。
    """
    n = dict(raw)
    if not n.get("app_token") and n.get("token"):
        n["app_token"] = n["token"]
    if not n.get("phone") and n.get("userCheckId"):
        try:
            n["phone"] = str(int(str(n["userCheckId"]), 16))
        except ValueError:
            pass
    for snake, camel in _CAPTURE_KEY_MAP.items():
        if not n.get(snake) and n.get(camel):
            n[snake] = n[camel]
    return n


class Account:
    """单个账号配置。

    支持两种 JSON 写法（可混用，snake_case 优先）：
    - 抓包 appTokenLogin 请求体原文：token / userCheckId / provinceCode / cityCode ...
    - 归一化字段：app_token / phone / province_code / city_code ...
    """

    FIELDS = (
        "app_token", "phone", "province_code", "city_code",
        "carrier_operator", "app_version_code", "activity_id", "channel_id",
    )

    def __init__(self, raw: dict):
        env = os.environ
        g = lambda k, d="": raw.get(k, env.get("CMCC_" + k.upper(), d))
        self.app_token = g("app_token")
        self.phone = g("phone")
        self.province_code = g("province_code", "731")
        self.city_code = g("city_code", "0731")
        self.carrier_operator = raw.get("carrier_operator", "002")
        self.app_version_code = raw.get("app_version_code", "12.5.2")
        self.activity_id = g("activity_id", "1021122301")
        self.channel_id = g("channel_id", "P00000109876")
        # 供扩展模块/通知使用的配置文件路径
        self.config_path = ""

    @property
    def act_url(self) -> str:
        return f"{BASE}/qwhdhub/qwhdmark/{self.activity_id}?channelId={self.channel_id}"

    @property
    def tag(self) -> str:
        return "手机尾号 %s" % (self.phone[-4:] if self.phone else "????")

    def validate(self) -> str | None:
        if not self.app_token:
            return "缺少 token/app_token（抓包 appTokenLogin 请求体的 token 字段）"
        if not self.phone or not re.fullmatch(r"1\d{10}", self.phone):
            return "缺少合法手机号（抓包 JSON 需含 userCheckId 或 phone 字段）"
        return None


def _env_fallback_account() -> dict:
    """单账号独立环境变量兜底（CMCC_APP_TOKEN + CMCC_PHONE 等）。"""
    raw = {}
    for k in Account.FIELDS:
        v = os.environ.get("CMCC_" + k.upper())
        if v:
            raw[k] = v
    return raw


def load_accounts(config_path: str | None) -> tuple[list[Account], dict]:
    """加载账号列表。

    优先级：--config 文件 > CMCC_ACCOUNTS(JSON) > 独立环境变量。
    CMCC_ACCOUNTS / config.json 均接受：单个账号对象，或 {"accounts": [...]}，
    或顶层就是数组；亦兼容直接粘贴抓包 appTokenLogin 请求体 JSON。
    对象字段见 Account.FIELDS。
    """
    raws: list[dict] = []
    file_cfg = None
    if config_path and Path(config_path).exists():
        file_cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    elif os.environ.get("CMCC_CONFIG", "").strip():
        p = Path(os.environ["CMCC_CONFIG"].strip())
        if p.exists():
            file_cfg = json.loads(p.read_text(encoding="utf-8"))

    if isinstance(file_cfg, dict) and isinstance(file_cfg.get("accounts"), list):
        raws = [a for a in file_cfg["accounts"] if isinstance(a, dict)]
    elif file_cfg:
        raws = [file_cfg]
    else:
        env_raw = os.environ.get("CMCC_ACCOUNTS", "").strip()
        if env_raw:
            data = json.loads(env_raw)
            if isinstance(data, dict) and isinstance(data.get("accounts"), list):
                raws = [a for a in data["accounts"] if isinstance(a, dict)]
            elif isinstance(data, list):
                raws = [a for a in data if isinstance(a, dict)]
            elif isinstance(data, dict):
                raws = [data]
            else:
                raise ValueError("CMCC_ACCOUNTS 需为 JSON 对象或对象数组")
        else:
            fb = _env_fallback_account()
            if fb:
                raws = [fb]

    accounts = []
    for raw in raws:
        acc = Account(_normalize_capture(raw))
        acc.config_path = str(config_path) if config_path else ""
        accounts.append(acc)
    return accounts


# ---------------------------------------------------------------- 会话建立

def api_headers(referer: str) -> dict:
    """活动 API 的固定头（抓包还原，缺一可能被拦）。"""
    return {
        "accept": "*/*",
        "content-type": "application/json;charset=UTF-8",
        "origin": BASE,
        "referer": referer,
        "login-check": "1",
        "x-requested-with": "XMLHttpRequest",
    }


def jwt_cache_file(acc: Account) -> Path:
    """jwt 缓存按手机尾号隔离（jwt 与账号绑定，换号配置时防止用到旧账号的凭证）。"""
    return Path(__file__).with_name(f".cmcc_jwt_cache_{acc.phone[-4:]}.json")


def load_jwt_cache(acc: Account) -> tuple[str, float] | None:
    """返回 (jwt, 凭证链起点 first_seen)，无可用缓存时返回 None。"""
    try:
        data = json.loads(jwt_cache_file(acc).read_text(encoding="utf-8"))
        # 缓存记录签发时的手机号，与当前配置不一致则视为串号，弃用
        if data.get("phone") != acc.phone:
            return None
        if time.time() - data.get("saved_at", 0) < 30 * 86400:  # 至少 30 天内的缓存值得尝试
            jwt = data.get("jwt")
            if jwt:
                return jwt, data.get("first_seen") or data.get("saved_at", time.time())
    except Exception:
        pass
    return None


def save_jwt_cache(acc: Account, jwt: str | None):
    """回写最新 jwt。first_seen 记录凭证链起点（首次引导时间）并跨重签保持。"""
    if not jwt:
        return
    try:
        path = jwt_cache_file(acc)
        first_seen = time.time()
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("phone") == acc.phone and old.get("first_seen"):
                first_seen = old["first_seen"]
        except Exception:
            pass
        path.write_text(
            json.dumps({"jwt": jwt, "phone": acc.phone,
                        "first_seen": first_seen, "saved_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def exchange_session(acc: Account) -> tuple[requests.Session, str]:
    """走 SSO 换取活动会话（QWHD_SESSION_TOKEN 落在 session cookie 里）。

    返回 (session, referer)。referer 是带 token 的活动页地址，后续 API 都要带。

    凭证策略：jwt 是账号级长期凭证，续期时 jwtToken 与 app_token 同发
    （服务端凭 token 定位 APP 会话校验 jwt）；app_token 同时作为 jwt
    缺失/失效时的引导兜底；每次登录响应都会重签 jwt，回写缓存。
    """
    s = requests.Session()
    # 直连：不继承系统/环境变量代理，避免本机抓包工具的 MITM 证书干扰 SSL 验证
    s.trust_env = False
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({"user-agent": USER_AGENT, "accept-language": "zh-CN,zh-Hans;q=0.9"})

    # ① 登录中转页，提取一次性 sid
    r = s.get(SSO_LOGIN, params={"dlwmh": "true", "actUrl": acc.act_url}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    m_app = re.search(r"loginPath\s*=\s*'([^']+)'", r.text)
    if not m_app:
        raise RuntimeError("登录页未返回 sid，SSO 入口可能已变更")
    login_url = BASE + "/qwhdsso" + m_app.group(1)

    base_body = {
        "provinceCode": acc.province_code,
        "cityCode": acc.city_code,
        "userCheckId": format(int(acc.phone), "x"),  # 手机号转十六进制
        "carrierOperator": acc.carrier_operator,
        "appVersionCode": acc.app_version_code,
        "took": random.randint(120, 900),
    }

    # ② 优先用缓存 jwt 续期
    # 注意：jwtToken 必须与 token（app_token）同时携带——服务端凭 token 定位
    # APP 会话再校验 jwt，token 留空会报「APP登录状态已过期」（实测结论，
    # 与 App 抓包请求体 jwtToken+token 同发一致）。
    resp = None
    cached = load_jwt_cache(acc)
    if cached:
        jwt_str, jwt_first_seen = cached
        r = s.post(login_url, json={**base_body, "jwtToken": jwt_str,
                                    "token": acc.app_token}, timeout=REQUEST_TIMEOUT)
        try:
            cand = r.json()
        except ValueError:
            cand = {}
        if cand.get("code") == "SUCCESS":
            resp = cand
            log.info("[%s] jwt 续期成功（凭证链已 %.1f 天）",
                     acc.tag, (time.time() - jwt_first_seen) / 86400)
        else:
            log.info("[%s] jwt 续期失败(%s)，回落 appTokenLogin 引导",
                     acc.tag, cand.get("msg"))

    # ③ 回落：app_token 引导登录（首次配置或 jwt 失效后）
    if resp is None:
        r = s.post(login_url, json={**base_body, "jwtToken": None, "token": acc.app_token}, timeout=REQUEST_TIMEOUT)
        resp = r.json()
        if resp.get("code") != "SUCCESS":
            raise RuntimeError(
                f"appTokenLogin 失败: {resp.get('code')} {resp.get('msg')} —— "
                "通常是 app_token 过期，请重新抓包更新凭证"
            )

    data = resp["data"]
    save_jwt_cache(acc, data.get("jwt"))

    # ④ 访问带 token 的活动页，服务器 Set-Cookie: QWHD_SESSION_TOKEN
    # URL 来自服务端响应，按 SSRF 防护约束在 wx.10086.cn 域内，且不跟随重定向
    assert_safe_url(data["url"], allowed_hosts={"wx.10086.cn"})
    r = s.get(data["url"], timeout=REQUEST_TIMEOUT, allow_redirects=False)
    r.raise_for_status()
    # 不同 hub 落不同令牌名（qwhdhub → QWHD_SESSION_TOKEN，hlwyxhdhub → HLWHD_SESSION_TOKEN），
    # 同一 jwt 跨 hub 通用，故按后缀匹配
    if not any(n.endswith("SESSION_TOKEN") for n in s.cookies.keys()):
        raise RuntimeError("未取得活动会话令牌，会话兑换失败")
    token_name = next(n for n in s.cookies.keys() if n.endswith("SESSION_TOKEN"))
    log.info("[%s] 活动会话建立成功（%s=%.12s...）", acc.tag, token_name, s.cookies[token_name])
    return s, data["url"]


# ---------------------------------------------------------------- 业务接口

def query_markstatus(s: requests.Session, referer: str) -> dict:
    r = s.post(f"{API_MARK}/mark31/markstatus", json={}, headers=api_headers(referer), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") != "SUCCESS":
        raise PermissionError(f"markstatus 失败: {resp.get('code')} {resp.get('msg')}")
    return resp["data"]


def do_mark(s: requests.Session, referer: str, date: str) -> dict:
    r = s.post(f"{API_MARK}/mark31/domark", json={"date": date}, headers=api_headers(referer), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def claim_task_awards(s: requests.Session, referer: str, status_data: dict) -> list[str]:
    """领取 taskAwardChance 里的累计/连签奖励（热门奖品库存紧张，领不到属正常）。

    服务端把"当前可领"的奖励算好放在 taskAwardChance，领取后条目即从清单消失，
    重复运行天然幂等。领取成功时奖品名在响应 data.prizeName 里。
    """
    names = {}
    pools = [status_data.get("taskAwardChance") or [],
             status_data.get("accumulateTaskInfo") or [],
             status_data.get("myTaskInfo") or []]
    for pool in pools:
        for t in pool:
            tid = t.get("id")
            if not tid or names.get(tid):
                continue
            name = (t.get("prize") or {}).get("name") or t.get("lotteryText") or t.get("prizeAlertText") or ""
            if name:
                names[tid] = name

    def label(task: dict) -> str:
        tid = task.get("id") or "?"
        bits = []
        if task.get("taskType") == "accumulate" and task.get("num"):
            bits.append(f"累计{task['num']}签")
        if names.get(tid):
            bits.append(names[tid])
        return f"任务{tid}" + (f"（{'·'.join(bits)}）" if bits else "")

    results = []
    for task in status_data.get("taskAwardChance") or []:
        tid = task.get("id")
        if not tid:
            continue
        r = s.post(f"{API_MARK}/mark31/taskAward/{tid}", json={}, headers=api_headers(referer), timeout=REQUEST_TIMEOUT)
        resp = r.json()
        data = resp.get("data") or {}
        if isinstance(data, dict) and data.get("prizeName"):
            value = data.get("prizeValue")
            unit = {"FLOW": "MB", "FEE": "元"}.get(data.get("prizeCategory"), "")
            amount = f"{value}{unit}" if value else ""
            results.append(f"{label(task)} 领到: {data['prizeName']}" + (f"（{amount}）" if amount else ""))
        else:
            status_text = resp.get("status") or resp.get("code") or "?"
            results.append(f"{label(task)}: {status_text} {resp.get('msg')}")
        time.sleep(random.uniform(1, 2))
    return results


def parse_prize(mark_result: dict) -> str:
    """从 domark 响应里提取奖品描述。"""
    prize = (mark_result.get("data") or {}).get("markPrize")
    if not prize:
        return ""
    parts = [prize.get("name") or ""]
    if prize.get("prizeValue"):
        parts.append(f"{prize['prizeValue']}{'MB' if prize.get('prizeCategory') == 'FLOW' else '元'}")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- 单账号执行

def run_account(acc: Account, dry_run: bool, claim: bool) -> tuple[bool, list[str]]:
    """执行单账号签到全流程，返回 (是否成功, 结果文案行)。"""
    today = datetime.now().strftime("%Y%m%d")
    lines = []

    # 重建会话的兜底：第一次失败若是会话问题，重建后再试一次
    status_data = None
    for attempt in (1, 2):
        try:
            s, referer = exchange_session(acc)
            status_data = query_markstatus(s, referer)
            break
        except PermissionError as e:
            if attempt == 2:
                raise
            log.warning("[%s] 会话异常(%s)，重建后重试", acc.tag, e)

    userinfo = status_data.get("userinfo") or {}
    acc_days = userinfo.get("accumulateTimes", "?")
    signed_today = any(
        d.get("date") == today and d.get("status") == "1"
        for d in status_data.get("markstatus") or []
    )
    log.info("[%s] 当前累计签到 %s 天，今日%s", acc.tag, acc_days, "已签" if signed_today else "未签")

    ok = True
    if signed_today:
        lines.append(f"今日已签到（累计 {acc_days} 天），无需操作")
    elif dry_run:
        lines.append(f"[dry-run] 今日未签，累计 {acc_days} 天")
    else:
        result = do_mark(s, referer, today)
        code, msg, status = result.get("code"), result.get("msg", ""), result.get("status", "")
        log.info("[%s] domark 响应: code=%s status=%s msg=%s", acc.tag, code, status, msg)
        # HAVE_MARKED 是服务端幂等保护（实测重复签到返回该码），视为已签成功
        if code == "SUCCESS" or "已签" in msg or status == "HAVE_MARKED":
            prize = parse_prize(result)
            try:
                new_data = query_markstatus(s, referer)
                acc_days = (new_data.get("userinfo") or {}).get("accumulateTimes", acc_days)
                status_data = new_data
            except Exception:
                pass
            if status == "HAVE_MARKED":
                lines.append(f"今日已签到过（服务端幂等），累计 {acc_days} 天")
            else:
                lines.append(f"签到成功！累计 {acc_days} 天")
                if prize:
                    lines.append(f"获得奖品: {prize}")
                elif status == "PRIZE_NO_CONFIG":
                    lines.append("今日无单日奖品（累计/连签奖励见下方领奖结果）")
        else:
            lines.append(f"签到失败: {code} / {status} / {msg}")
            ok = False

    # 领奖：门槛达标的累计/连签奖励不会自动发放，默认代领。
    # dry-run 下只报「可领未领」，不做任何写操作。
    if claim and status_data is not None:
        try:
            latest = status_data if signed_today else query_markstatus(s, referer)
            chances = latest.get("taskAwardChance") or []
            if dry_run:
                for t in chances:
                    lines.append(f"[领奖] [dry-run] 发现可领未领奖励: 任务{t.get('id')}"
                                 f"（累计{t.get('num')}签，正式运行将自动领取）")
                if not chances:
                    lines.append("[领奖] 当前无可领取的累计/连签奖励")
            elif not chances:
                log.info("[%s] [领奖] 当前无可领取的累计/连签奖励", acc.tag)
            else:
                for line in claim_task_awards(s, referer, latest):
                    log.info("[%s] [领奖] %s", acc.tag, line)
                    lines.append(f"[领奖] {line}")
        except Exception as e:
            lines.append(f"[领奖] 尝试失败: {e}")

    return ok, lines


# ---------------------------------------------------------------- 通知

def push_feishu(sections, overall_ok):
    """推送到飞书自定义机器人。sections: [(账号名, [文案行...]), ...]。"""
    color = "green" if overall_ok else "red"
    lines = []
    for name, acc_lines in sections:
        if lines:
            lines.append("")
        if name:
            lines.append("**%s**" % name)
        for line in acc_lines:
            lines.append(line)
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
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=REQUEST_TIMEOUT)
            body = r.json()
            log.info("[飞书] 响应：%s", json.dumps(body, ensure_ascii=False))
            if body.get("code") == 0:
                return True, ""
            return False, ("飞书返回码 %s（19001 多为地址不对；机器人若开启签名校验也会失败，"
                           "建机器人时不要勾选）" % body.get("code", "未知"))
        except Exception as e:
            if attempt < MAX_RETRY:
                log.warning("[飞书] 请求异常，退避后重试（%d/%d）：%s: %s",
                            attempt, MAX_RETRY, type(e).__name__, e)
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return False, "重试 %d 次后仍异常：%s: %s" % (MAX_RETRY, type(e).__name__, e)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动 App 签到自动脚本")
    parser.add_argument("--config", default="", help="配置文件路径（默认 config.json，若存在）")
    parser.add_argument("--dry-run", action="store_true", help="只查询状态，不执行签到/领奖")
    parser.add_argument("--claim", dest="claim", action="store_true", default=True,
                        help="签到后自动领取累计/连签奖励（默认开启）")
    parser.add_argument("--no-claim", dest="claim", action="store_false", help="跳过领奖，只签到")
    parser.add_argument("--delay", type=int, default=0, metavar="N", help="启动前随机延迟 0~N 秒")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.delay > 0:
        wait = random.uniform(0, args.delay)
        log.info("随机延迟 %.0f 秒后开始...", wait)
        time.sleep(wait)

    config_path = args.config or os.environ.get("CMCC_CONFIG", "").strip()
    if not config_path and Path("config.json").exists():
        config_path = "config.json"

    try:
        accounts = load_accounts(config_path or None)
    except Exception as e:
        log.error("配置解析失败: %s", e)
        return 1

    if not accounts:
        log.error("未配置任何账号：请设置 CMCC_ACCOUNTS（JSON）或 "
                  "CMCC_APP_TOKEN/CMCC_PHONE，或提供 --config config.json")
        return 1

    # 校验全部账号
    bad = []
    for acc in accounts:
        err = acc.validate()
        if err:
            bad.append((acc.tag, err))
    if bad:
        for tag, err in bad:
            log.error("[%s] %s", tag, err)
        log.error("参考 config.example.json 填写，或设置对应环境变量")
        return 1

    sections = []
    overall_ok = True
    single = len(accounts) == 1
    for acc in accounts:
        name = "" if single else acc.tag
        log.info("[%s] 开始签到…", name or "单账号")
        try:
            ok, lines = run_account(acc, dry_run=args.dry_run, claim=args.claim)
        except Exception as e:
            log.exception("[%s] 执行失败: %s", acc.tag, e)
            ok, lines = False, [f"执行异常: {e}"]
        log.info("[%s] 结果：\n%s", acc.tag, "\n".join(lines))
        sections.append((name, lines))
        if not ok:
            overall_ok = False

    # 汇总写入结果文件（供 Actions step summary 使用）
    if RESULT_FILE:
        try:
            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                for name, lines in sections:
                    if name:
                        f.write("- %s\n" % name)
                    for line in lines:
                        f.write("  - %s\n" % line)
        except OSError as e:
            log.warning("写入结果文件失败：%s", e)

    if FEISHU_WEBHOOK:
        ok, reason = push_feishu(sections, overall_ok)
        log.info("[飞书] %s", "推送成功" if ok else "推送失败：%s" % reason)
    else:
        log.info("未配置 FEISHU_WEBHOOK，跳过飞书推送。")

    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(main())
