# -*- coding: utf-8 -*-
"""中国移动 App「周三充值日」抢券（hlwyxhdhub drawPrize）自动化。

接口链路（wx.10086.cn / hlwyxhdhub wedrecharge 活动，移植自 cmcc-auto-checkin/cmc_wed.py）：

  1. exchange_session(act_url=WED_ACT_URL)   SSO 换 HLWHD_SESSION_TOKEN（进页即每日登录）
  2. queryDrawPrizeInfo                      服务器时间 + 场次/奖品信息（含 constId）
  3. 等到目标场次（服务器时钟校准，开火前刷新会话保活）
  4. drawPrize {"constId": ...}              连发抢券，命中/库存尽/打完预算即停

constId 为抓包确认的请求体字段：它是前端 _zw 风控 SDK 生成的设备指纹
（window.__constId），非服务端下发，按设备长期有效；账号 JSON 里可用
wed_const_id/constId 覆盖，缺省用抓包样本值。

用法（由 cmcc_auto.py --wed 调用，也可独立运行）:
  python cmcc_wed.py --config config.json --dry-run   # 只查场次/奖品，不发抢购
  python cmcc_wed.py --config config.json              # 等最近场次开抢
  python cmcc_wed.py --at 08:00:00                     # 指定场次时间
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

import cmcc_auto as core
from cmcc_auto import BASE, Account, api_headers, exchange_session

log = logging.getLogger("cmcc_wed")

# 活动页地址（pageId/channelId 为活动公开配置；跟踪参数不需要）。
# 注意必须用 /1024101716 数字路径路由——index.html 页面路由访问不下发
# HLWHD_SESSION_TOKEN（实测），数字路径才是 SSO 正确入口。
WED_ACT_URL = ("https://wx.10086.cn/hlwyxhdhub/act-wedrecharge/1024101716"
               "?pageId=1849008675699650560&channelId=P00000132544")
API_WED = BASE + "/hlwyxhdhub/api/wedrecharge"

# 抓包样本 constId（2026-11-11 抓包）。constId 是前端 _zw 风控 SDK 生成的
# 设备指纹 ID（window.__constId，调 yxfk.market.chinamobile.com udid 接口），
# 非服务端下发、接口响应里也找不到，按设备长期有效，直接沿用抓包值。
FALLBACK_CONST_ID = "6af4012fDavEcUd5WdN1uW2TOGar1etCuD0f9671"

SEK_STOP_CODES = {"160002"}   # 奖品库存不足
SEK_SUCCESS_CODE = "0"


def wed_api(s, referer: str, path: str, payload: dict | None = None) -> dict:
    """调 wedrecharge API。本 hub 的成功码是字符串 "0"（区别于 qwhdhub 的 SUCCESS）。"""
    r = s.post(f"{API_WED}/{path}", json=payload or {},
               headers=api_headers(referer), timeout=core.REQUEST_TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") != "0":
        raise RuntimeError(f"{path} 失败: {resp.get('code')} {resp.get('msg')}")
    return resp.get("data") or {}


def server_time(s, referer: str) -> tuple[float, dict]:
    """取服务器时钟与场次信息（queryDrawPrizeInfo 带 currentTime）。"""
    d = wed_api(s, referer, "queryDrawPrizeInfo")
    return d["currentTime"] / 1000, d


def _sk_hours(rec: dict) -> tuple[list[int], str]:
    """场次小时列表。seckillTime 服务端未下发时按 App 显示取 08:00。"""
    sk_raw = rec.get("seckillTime")
    if sk_raw:
        return [int(h) for h in sk_raw], "服务端下发"
    return [8], "服务端未下发，按 App 显示取 08:00"


def _prize_lines(nfos: list) -> list[str]:
    return ["秒杀奖品: " + "；".join(
        f"{p.get('name', '')}" + (f"（{p['subTitle']}）" if p.get("subTitle") else "")
        for p in nfos)] if nfos else []


def seckill_run(acc: Account, dry_run: bool, at: str | None = None,
                lead: float = 0.4, interval: float = 0.35,
                max_attempts: int = 120, force: bool = False) -> list[str]:
    """周三充值日抢券：等服务器时间到场次点，连发 drawPrize 直到命中或打完预算。"""
    s, referer = exchange_session(acc, act_url=WED_ACT_URL)

    def fresh_session():
        nonlocal s, referer
        s, referer = exchange_session(acc, act_url=WED_ACT_URL)

    # constId 是前端 _zw 风控 SDK 的设备指纹（非服务端下发）：
    # 账号配置 wed_const_id/constId > 抓包样本兜底
    const_id = getattr(acc, "wed_const_id", "") or FALLBACK_CONST_ID
    log.info("[秒杀] constId: %s", const_id[:20] + ("..." if len(const_id) > 20 else ""))

    now_sv, info = server_time(s, referer)
    skew_ms = (now_sv - time.time()) * 1000
    now_dt = datetime.fromtimestamp(now_sv)
    weekday_cn = "一二三四五六日"[now_dt.weekday()]

    try:
        rec = s.get(f"{API_WED}/_pub/rec_login", timeout=core.REQUEST_TIMEOUT).json().get("data") or {}
    except Exception:
        rec = {}
    sk_hours, sess_note = _sk_hours(rec)
    nfos = rec.get("Seckillnfos") or []

    lines = [f"服务器时间 {now_dt.strftime('%m-%d %H:%M:%S')}，本机时钟偏移 {skew_ms:+.0f}ms"]
    sess_lines = []
    for h in sk_hours:
        t = now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        sess_lines.append(f"  {h:02d}:00" + ("（已过）" if t.timestamp() < now_sv else ""))
    lines.append(f"共 {len(sk_hours)} 个场次（{sess_note}）:\n" + "\n".join(sess_lines))
    lines += _prize_lines(nfos)

    # 选定目标场次：--at 优先，否则取最近一个未开始的场次
    if at:
        hh, mm, ss = (int(x) for x in at.split(":"))
        target = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        src = f"--at {at}"
    else:
        future = [h for h in sk_hours
                  if now_dt.replace(hour=h, minute=0, second=0, microsecond=0).timestamp() >= now_sv]
        if not future:
            lines.append("今日场次已全部结束，明日再跑（或 --at 指定场次）")
            return lines
        target = now_dt.replace(hour=future[0], minute=0, second=0, microsecond=0)
        src = f"场次 {future[0]:02d}:00"
    if target.timestamp() < now_sv - 3600:  # 已过目标 1 小时以上则等明天
        target += timedelta(days=1)
        src += "+1天"

    # 场次在周三（活动规则），非周三等待只会到点空打
    if target.weekday() != 2 and not force:
        lines.append(f"目标日 {target.strftime('%m-%d')} 为周"
                     f"{'一二三四五六日'[target.weekday()]}，非周三；"
                     "确认活动改期可加 --force 强制等待")
        return lines

    away = int(target.timestamp() - now_sv)
    lines.append(f"选定目标：{target.strftime('%m-%d %H:%M:%S')}（{src}，周{weekday_cn}），"
                 f"距开始 {away} 秒；提前 {lead}s 开火，间隔 {interval}s，预算 {max_attempts} 发")

    for ln in lines:   # 启动信息实时输出，等待期间控制台不再静默
        log.info(ln)

    if dry_run:
        lines.append("[dry-run] 不发抢购请求")
        return lines

    # 等待到点（含 lead 提前量）。HLWHD 会话 Cookie 仅 30 分钟有效：
    # >150s 每 30s 校时（失败重建会话）；150s~30s 强制重建；≤30s 贴步等待
    deadline_local = time.time() + max(0.0, target.timestamp() - lead - now_sv)
    refreshed = False
    while True:
        remain = deadline_local - time.time()
        if remain <= 0:
            break
        if refreshed and remain > 30:
            time.sleep(min(remain - 30, 20))
            continue
        if remain > 150:
            time.sleep(min(30, remain - 150))
            if not refreshed:
                try:
                    now_sv, _ = server_time(s, referer)
                    deadline_local = time.time() + max(0.0, target.timestamp() - lead - now_sv)
                except Exception:
                    fresh_session()
            continue
        if not refreshed and remain > 30:
            fresh_session()   # 开火前刷新会话，保证到点时 Cookie 有效
            refreshed = True
            continue
        time.sleep(min(0.2, remain))

    payload = {"constId": const_id} if const_id else {}
    last, fired = "", 0
    for i in range(1, max_attempts + 1):
        fired = i
        t0 = time.monotonic()
        try:
            r = s.post(f"{API_WED}/drawPrize", json=payload,
                       headers=api_headers(referer), timeout=10)
            resp = r.json()
        except Exception as e:
            resp = {"code": "EXC", "msg": str(e)}
        code, msg = str(resp.get("code")), resp.get("msg") or ""
        log.info("[秒杀] 第%d发: %s %s", i, code, msg)
        if code == SEK_SUCCESS_CODE:
            prizes = (resp.get("data") or {}).get("actPrizes") or []
            for p in prizes:
                lines.append(f"[秒杀] 抢中: {p.get('prizeName') or p.get('shortName') or ''}"
                             + (f"（{p['subTitle']}）" if p.get("subTitle") else ""))
            break
        last = f"{code} {msg}"
        if code in SEK_STOP_CODES:
            lines.append(f"[秒杀] 停止: {last}")
            break
        remain = interval - (time.monotonic() - t0)
        if remain > 0:
            time.sleep(remain)
    else:
        lines.append(f"[秒杀] {max_attempts} 发打完未中，最后响应: {last}")

    lines.append(f"[秒杀] 共尝试 {fired} 发")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动「周三充值日」抢券脚本")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只查场次/奖品，不发抢购")
    parser.add_argument("--at", default=None, help="场次时间（服务器时间，如 08:00:00）")
    parser.add_argument("--force", action="store_true", help="目标日非周三也强制等待")
    parser.add_argument("--lead", type=float, default=0.4, help="提前开火秒数（默认 0.4）")
    parser.add_argument("--interval", type=float, default=0.35, help="连发间隔秒（默认 0.35）")
    parser.add_argument("--max-attempts", type=int, default=120, help="最多连发次数（默认 120）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    accounts = core.load_accounts(args.config or None)
    if not accounts:
        log.error("未配置账号")
        return 1
    for acc in accounts:
        err = acc.validate()
        if err:
            log.error("[%s] %s", acc.tag, err)
            return 1

    overall_ok = True
    sections = []
    for acc in accounts:
        name = "" if len(accounts) == 1 else acc.tag
        log.info("[%s] 周三充值日抢券…", name or "单账号")
        try:
            lines = seckill_run(acc, dry_run=args.dry_run, at=args.at,
                                lead=args.lead, interval=args.interval,
                                max_attempts=args.max_attempts, force=args.force)
            ok = not any("抢中" not in l and ("失败" in l or "异常" in l) for l in lines)
        except Exception as e:
            log.exception("[%s] 执行失败: %s", acc.tag, e)
            ok, lines = False, [f"执行异常: {e}"]
        log.info("[%s] 结果：\n%s", acc.tag, "\n".join(lines))
        sections.append((name, lines))
        if not ok:
            overall_ok = False

    if core.FEISHU_WEBHOOK:
        ok, reason = core.push_feishu(sections, overall_ok)
        log.info("[飞书] %s", "推送成功" if ok else "推送失败：%s" % reason)
    return 0 if overall_ok else 2


if __name__ == "__main__":
    sys.exit(main())
