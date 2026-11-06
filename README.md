# 中国移动自动签到（GitHub Actions + 飞书推送）

对接「中国移动 签到有礼-微信端」接口：
`POST https://wx.10086.cn/qwhdhub/api/mark/do/mark`（仅需在请求头带 Cookie）。

- 纯 Python 标准库，无第三方依赖，单文件 `cmcc_auto.py`
- GitHub Actions 每日定时签到（两个时段互为备份，脚本幂等，重复触发只会提示「今日已签」）
- 结果推送到飞书自定义机器人（可选），支持多账号

## 接口说明

| 项 | 值 |
|---|---|
| 请求方式 | POST |
| URL | `https://wx.10086.cn/qwhdhub/api/mark/do/mark` |
| 鉴权 | 请求头 `Cookie`（固定值，抓包获取） |

成功响应（节选）：

```json
{
  "code": "SUCCESS",
  "msg": "成功",
  "success": true,
  "data": {
    "prizeName": "100MB日包",
    "categoryOverview": { "valueDesc": "1.09GB" }
  }
}
```

已签响应：`{"code": "TODAY_MARKED", "msg": "今日已签", "success": false}`（视为成功，不报错）。

## 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `CMCC_COOKIE` | ✅ | 签到接口的 Cookie。抓包（微信里打开活动页，Fiddler / Charles 抓 `wx.10086.cn` 请求）复制完整 Cookie 字符串。多账号用换行分隔 |
| `FEISHU_WEBHOOK` | 可选 | 飞书群自定义机器人 Webhook（建机器人时不要勾选签名校验） |

> Cookie 有有效期，失效后签到会返回 HTTP 200 但 `success: false` 或非 200，届时重新抓一次更新 Secret 即可。

## 飞书机器人

1. 目标群 → 设置 → 群机器人 → 添加「自定义机器人」
2. 安全设置**不要勾选**「签名校验」（可选关键词或 IP 白名单）
3. 复制 Webhook 地址填入 Secret `FEISHU_WEBHOOK`

推送为交互式卡片，成功绿色、有失败项红色，逐账号展示结果。

## 运行时机

`.github/workflows/daily.yml` 内置两个 cron（UTC，均为每日）：

- `22:10 UTC` = 北京 06:10（主时段）
- `14:10 UTC` = 北京 22:10（备份）

可在 Actions 页面手动 `Run workflow` 立即测试。

## 本地调试

```bash
# PowerShell
$env:CMCC_COOKIE="你的Cookie"
$env:FEISHU_WEBHOOK="你的Webhook"   # 可选
python cmcc_auto.py
```
