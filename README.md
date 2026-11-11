# 中国移动自动签到（GitHub Actions + 飞书推送）

对接中国移动 App「签到领流量/话费」接口（wx.10086.cn / qwhdhub mark31 活动）：

1. `GET /qwhdsso/login` 取登录中转页 → `POST /qwhdsso/appTokenLogin` 用 App 票据（或缓存 jwt）换活动页跳转 URL
2. 访问带 token 的活动页，服务端下发 `QWHD_SESSION_TOKEN` 会话
3. `POST /qwhdhub/api/mark/mark31/markstatus` 查状态 → `domark` 签到 → `taskAward/<id>` 领累计/连签奖励

- 签到 + 自动领奖，脚本幂等，今日已签只会提示「今日已签到」
- jwt 为账号级长期凭证：首次用 `app_token` 引导后自动缓存续期，无需频繁重新抓包
- GitHub Actions 每日定时签到（两个时段互为备份）
- 结果推送到飞书自定义机器人（可选），支持多账号
- 依赖：仅 `requests`（见 requirements.txt）

## 配置

凭证直接使用中国移动 App 抓包数据：抓 `appTokenLogin` 请求（POST
`/qwhdsso/appTokenLogin`），把**请求体 JSON 原文**整个粘贴进来即可，脚本自动解析：

```json
{
  "provinceCode": "200",
  "cityCode": "0759",
  "appVersionCode": "12.5.7",
  "userCheckId": "44385a489",
  "carrierOperator": "002",
  "jwtToken": null,
  "token": "JSESSIONID=...; UID=...; ticketID=SuZhou; Secure",
  "took": 251
}
```

字段映射：`token` → App 票据；`userCheckId`（手机号十六进制）→ 手机号；
`provinceCode`/`cityCode`/`carrierOperator`/`appVersionCode` 原样使用；
`jwtToken`/`took` 忽略（jwt 由脚本缓存续期）。

三种配置方式（优先级：`--config` 文件 > `CMCC_ACCOUNTS` > 独立环境变量）：

**GitHub Actions（推荐）**：建 Secret `CMCC_ACCOUNTS`，直接粘贴抓包 JSON。
多账号填 JSON 数组，或 `{"accounts": [...]}` 包裹：

```json
[
  { "token": "JSESSIONID=...（账号1）", "userCheckId": "...", "cityCode": "0759", "...": "..." },
  { "token": "JSESSIONID=...（账号2）", "userCheckId": "...", "cityCode": "0731", "...": "..." }
]
```

**本地调试**：复制 `config.example.json` 为 `config.json`（已 gitignore）粘贴抓包
JSON，或直接设环境变量 `CMCC_APP_TOKEN` / `CMCC_PHONE` 等。

## Secrets 列表

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `CMCC_ACCOUNTS` | ✅ | 抓包 `appTokenLogin` 请求体 JSON 原文；多账号为 JSON 数组 |
| `FEISHU_WEBHOOK` | 可选 | 飞书群自定义机器人 Webhook（不要勾选签名校验） |

> `app_token` 即 App 登录会话凭证：App 保持登录则长期有效，jwt 续期请求会同时携带它；App 重新登录/退出后需重新抓包更新。jwt 缓存失效且 app_token 也失效时签到会报「appTokenLogin 失败」。

## 飞书机器人

1. 目标群 → 设置 → 群机器人 → 添加「自定义机器人」
2. 安全设置**不要勾选**「签名校验」
3. 复制 Webhook 地址填入 Secret `FEISHU_WEBHOOK`

推送为交互式卡片，成功绿色、有失败项红色，逐账号展示结果。

## 运行时机

`.github/workflows/daily.yml` 内置两个 cron（UTC，均为每日）：

- `22:10 UTC` = 北京 06:10（主时段）
- `14:10 UTC` = 北京 22:10（备份）

可在 Actions 页面手动 `Run workflow` 立即测试。

## 本地调试

```bash
pip install -r requirements.txt

# 方式一：环境变量（PowerShell）
$env:CMCC_APP_TOKEN="JSESSIONID=...; UID=...; ticketID=..."
$env:CMCC_PHONE="15100000000"
$env:FEISHU_WEBHOOK="你的Webhook"   # 可选
python cmcc_auto.py

# 方式二：config.json
python cmcc_auto.py --config config.json
python cmcc_auto.py --dry-run        # 只查状态，不签到不领奖
python cmcc_auto.py --no-claim       # 只签到，不领奖
python cmcc_auto.py --delay 600      # 启动前随机延迟 0~600 秒
```
