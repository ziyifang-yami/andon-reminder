# Andon Cord Notification — 企微消息通知说明

## 目录

- [什么是 Andon Cord](#什么是-andon-cord)
- [通知触发机制总览](#通知触发机制总览)
- [Channel 1 — 实时通知（BPM Service 触发）](#channel-1--实时通知bpm-service-触发)
- [Channel 2 — 定时提醒（Python 脚本）](#channel-2--定时提醒python-脚本)
- [消息格式对比](#消息格式对比)
- [脚本说明](#脚本说明)
- [用户映射逻辑](#用户映射逻辑)
- [配置参考](#配置参考)
- [错误处理](#错误处理)
- [已知未映射账号](#已知未映射账号)

---

## 什么是 Andon Cord

Andon Cord（ADC）是仓库运营 BPM 系统中的工单模块，用于追踪商品质量问题。  
当 CS 或仓库员工发现订单商品异常时，创建 ADC 工单，工单按类别分配给对应处理团队流转处理。

**工单类别（category2）：**

| category2 | 中文 | 英文 | 负责团队 |
|-----------|------|------|---------|
| 1 | 发霉变质 | Moldy/spoiled item | FC → ICQA |
| 2 | 临期过期 | Near expiration/expired | FC → ICQA（**触发机器人通知**） |
| 3 | 其他质量问题 | Poor quality item | FC → ICQA |
| 4 | 商品描述错误 | Item description | Design |
| 5 | 商品错发 | Wrong item | FC → ICQA |
| 6 | 保质期查询 | Expiration date inquiry | - |
| 7 | 商品信息查询 | Item information inquiry | - |
| 8 | 无订单查询 | No order# | - |
| 9 | 商品漏发 | Miss item | FC → ICQA（**触发机器人通知**） |
| 21 | 图片与实物不符 | Item picture | Design |
| 41–44 | 业务相关 | Retail issues | Buyer team |

**工单处理人（Processor）：**

| Code | 名称 | 说明 |
|------|------|------|
| 1 | CS | Customer Service，工单发起方 |
| 2 | FC-LA / FC-NJ / FC-ON | 仓库，按 warehouse_number 动态分配 |
| 3 | ICQA | 质量审查 |
| 4 | 1P-Food / 1P-NonFood / 3P | Buyer 团队 |
| 5 | Design | 图片团队 |
| 6 | View | 仅可查看 |

---

## 通知触发机制总览

```
ADC 工单生命周期中有两个独立的通知渠道：

  [BPM Service — Java]              [定时任务 — Python]
  ┌─────────────────────┐           ┌──────────────────────────┐
  │ 工单创建/更新 时      │           │ 定时扫描未处理工单         │
  │ 立即触发通知          │           │ (cron: 每日 08:00 PT)    │
  └────────┬────────────┘           └─────────────┬────────────┘
           │                                      │
           ▼                                      ▼
  ┌─────────────────────┐           ┌──────────────────────────┐
  │ Channel A:          │           │ andon_wecom_push.py      │
  │ WeCom 群机器人 Webhook│           │ → 全员提醒（AM/PM/CM/FC）  │
  │ (OPS_2 / OPS_9 专用)│           │                          │
  ├─────────────────────┤           │ andon_am_push.py         │
  │ Channel B:          │           │ → AM 专项提醒              │
  │ WeCom 应用消息       │           │                          │
  │ 发送给 ticket 处理人 │           │ andon_pm_cm_push.py      │
  └─────────────────────┘           │ → PM/CM 专项提醒           │
                                    └──────────────────────────┘
```

---

## Channel 1 — 实时通知（BPM Service 触发）

### 来源

`central-bpm-service` → `WxSendService.java`

### Channel A：群机器人 Webhook（仓库专属）

**触发条件：** 工单创建时，且 category2 为以下两种之一：
- `OPS_9`（category2 = 9）— 商品漏发 Miss item
- `OPS_2`（category2 = 2）— 临期过期 Near expiration/expired

**调用链：**

```
POST /andonCord/create
  └─ AndonCordService.createRecord()
       └─ createAndonCord() [@Transactional]
            └─ createAdcSendMessage()  ← 异步线程池执行
                 └─ WxSendService.createAdcSendMessage()
                      ├─ 按 warehouse_number 查 Apollo WarehouseValueConfig
                      │  获取对应仓库群机器人 URL
                      └─ sendMessageRobot()
                           └─ HTTP POST → qyapi.weixin.qq.com/cgi-bin/webhook/send
```

**消息体格式（Markdown）：**

```json
{
  "msgtype": "markdown",
  "markdown": {
    "content": "### ADC Miss item消息提醒\n>**事项详情**\n>工单号：<font color=\"info\">ADC2608210007</font>\n>操作类型：完成\n>操作人：tina.jia(498)\n>订单：2026082099730\n>SKU：1018051611\n>问题描述：客户反映商品漏发\n>点击查看：[工单详情](https://central.yamibuy.net/wh-v2/...)"
  }
}
```

| 字段 | OPS_9（漏发） | OPS_2（临期） |
|------|-------------|-------------|
| 标题 | `### ADC Miss item消息提醒` | `### ADC Near expiration/expired消息提醒` |
| 操作类型 | 完成 | 创建 |

**配置来源（Apollo）：**

| Key | 说明 |
|-----|------|
| `WH_MISS_ITEM_ROBOT_URL_MAP` | 仓库编号 → 群机器人 Webhook URL 的 Map（如 `001 → LA 群`, `002 → NJ 群`） |
| `adc_web_url` | 工单详情页前缀，默认 `https://gqc-central.yamibuy.tech/wh-v2/index.html#/wh/andonCordDetail?ticketNo=` |

---

### Channel B：WeCom 应用消息（发给个人）

**触发条件：** 工单更新/流转时，通知当前处理人（包括流转、状态变更、Reopen 等操作）

**调用链：**

```
POST /andonCord/update  (或 /updaterole / /updateStatus / /reopen)
  └─ AndonCordService.updateRecord()
       └─ sendMessage()
            ├─ 查处理人 email（xysc_admin_user.email）
            └─ WxSendService.sendMessageByAdc(email, andonCord)
                 ├─ getWxAccessToken()
                 │    GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=...&corpsecret=...
                 ├─ getUserIdByEmail(email)
                 │    POST https://qyapi.weixin.qq.com/cgi-bin/user/get_userid_by_email
                 └─ message/send
                      POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=...
```

流转操作（CIRCULATE）还会额外调用：
```
sendCirculateNotifyToProcessor(emails, andonCord)
  └─ 遍历 emails → sendMessageByAdc(email, andonCord)
```

**消息体格式（应用 Markdown）：**

```json
{
  "touser": "<wecom_userid>",
  "msgtype": "markdown",
  "agentid": 1000058,
  "markdown": {
    "content": "### Andon Cord Notification\n>**Ticket Detail**\n>Ticket No: <font color=\"info\">ADC2608210007</font>\n>Operation Type: Circulate\n>Operated by: tina.jia(498)\n>Memo: 已确认漏发，请仓库处理\n>Message: \n>View detail: [Ticket Detail](...)"
  }
}
```

**WeCom 应用配置（Apollo）：**

| Key | 默认值 | 说明 |
|-----|-------|------|
| `wx_url` | `https://qyapi.weixin.qq.com/cgi-bin/` | WeCom API base URL |
| `corp_id` | `ww6ffacca2e05d76b0` | 企业 ID |
| `corp_secret` | - | 应用 Secret |
| `agent_id` | `1000058` | 应用 AgentID |

---

## Channel 2 — 定时提醒（Python 脚本）

这些脚本独立于 BPM Service，直接读 MySQL，通过企微群机器人 Webhook 发送待处理工单汇总。  
由 cron / systemd timer 定时执行，用于提醒各团队处理积压工单。

### 三个脚本及适用场景

| 脚本 | 目标人群 | 触发时机建议 |
|------|---------|------------|
| `andon_wecom_push.py` | AM / PM / CM 全员汇总 | 工作日上午（LA 时间） |
| `andon_am_push.py` | AM（Account Management）专项 | 每日上午 |
| `andon_pm_cm_push.py` | PM / CM，支持按区域分组推送 | 每日上午，可按 us-west / us-east / apac 分组 |

### 数据关联路径

```
MySQL: yamibuy_wh.wh_bpm_andon_cord
  JOIN yamibuy_wh.wh_bpm_processor       ← 找到处理人 user_id
  JOIN yamibuy_master.xysc_admin_user     ← 获取 email
  JOIN yami_devops.wecom_user_mapping     ← 匹配企微 userid（用于 @）
                 （三级 fallback 匹配，见下方）
```

### andon_wecom_push.py

- 查所有状态不为 7（已完成）的工单
- 只处理**单处理人**的 ticket（多处理人单独列出，不 @）
- 按部门分组（AM / PM / CM / FC / Design）
- `text` 类型消息，`mentioned_list` 触发 @ 通知

```bash
# 直接运行
python andon_wecom_push.py

# 自定义 webhook
ANDON_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY" \
  python andon_wecom_push.py
```

### andon_am_push.py

- 只查 status = 4（待BU处理）的工单
- 通过 `yami_devops.user_mapping.wecom_department` 过滤 AM 部门
- 支持 `--dry-run` 参数（只打印不发送）
- `markdown` 类型消息，支持 `<@userid>` @人

```bash
python andon_am_push.py
python andon_am_push.py --dry-run
```

### andon_pm_cm_push.py

- 只查 status = 4 且 business_type = 1（1P 自营）的工单
- 自动区分 PM / CM（按职位关键词：PURCHASING / CATEGORY 等）
- 支持按地理区域分组推送
- `markdown` 类型消息

```bash
python andon_pm_cm_push.py               # 所有区域
python andon_pm_cm_push.py --group us-west   # 只推 LA
python andon_pm_cm_push.py --group us-east   # 只推 NJ + Canada
python andon_pm_cm_push.py --group apac      # 只推 Tokyo + Korea + Xi'an
```

---

## 消息格式对比

| 维度 | Channel A（群机器人） | Channel B（应用消息） | 定时提醒脚本 |
|------|-------------------|--------------------|------------|
| 触发时机 | 工单创建时（OPS_2/9） | 工单更新/流转时 | 定时 cron |
| 发送渠道 | 企微群机器人 Webhook | 企微应用消息（to user） | 企微群机器人 Webhook |
| 消息类型 | `markdown` | `markdown` | `text`（wecom_push）/ `markdown`（am/pm_cm） |
| @ 机制 | 无（机器人消息） | 点对点，无需 @ | `mentioned_list` 或 `<@userid>` |
| 覆盖范围 | 仓库群（按 warehouse） | 具体处理人 | 特定团队群 |
| 内容 | 单条工单详情 | 单条工单详情 | 待处理工单汇总 |

---

## 脚本说明

### 文件结构

```
Project-Z/andon-reminder/
  andon_wecom_push.py      ← 全员定时提醒（AM/PM/CM/FC）
  andon_am_push.py         ← AM 专项定时提醒
  andon_pm_cm_push.py      ← PM/CM 专项定时提醒（支持区域分组）
  andon_icqa_push.py       ← ICQA 专项提醒
  bpm_common.py            ← 部门映射公共模块
  config-example.json      ← Rust 侧配置示例（wecom-bot-kiro-cli 用）
  schema.sql               ← Rust 侧 andon.db schema（用于 Rust 定时轮询）
  README.md                ← 本文档
```

### 依赖

```
c:\DEV\wecom-bot-kiro-cli\workspace\skills\
  config\settings.py          ← MySQL engine (SQLAlchemy)
  bpm-ticket\scripts\         ← 公共查询模块
```

---

## 用户映射逻辑

Python 脚本通过三级 fallback 将 BPM 账号解析为企微 userid：

```
Level 1: LOWER(xysc_admin_user.email) = LOWER(wecom_user_mapping.email)   ← 精确匹配
Level 2: 归一化 user_name 与 wecom_user_mapping.display_name 匹配
         （Xina.Qu → "xina qu"）
Level 3: 归一化 user_name 与 wecom_user_mapping.userid 匹配
         （Xina.Qu → "xinaqu"）
```

`andon_am_push.py` 和 `andon_pm_cm_push.py` 还额外通过 `yami_devops.user_mapping`（Google → WeCom 映射表）进行关联，支持更准确的部门过滤。

**排除账号（EXCLUDED_UIDS）：**

| user_id | 说明 |
|---------|------|
| 26 | IT 系统账号（little） |
| 4355 | 测试账号 |
| 8864 | 测试账号 |

---

## 配置参考

### Python 脚本环境变量

| 变量 | 说明 | 默认值 |
|------|------|-------|
| `ANDON_WEBHOOK_URL` | 目标群机器人 Webhook URL | 各脚本内硬编码（测试群） |
| `SKILLS_DIR` | `workspace/skills` 路径 | 自动检测 Linux/Windows |

### BPM Service（Apollo 配置）

| Key | 说明 |
|-----|------|
| `WH_MISS_ITEM_ROBOT_URL_MAP` | 仓库号 → 群机器人 URL Map |
| `wx_url` | WeCom API 地址 |
| `corp_id` | 企业 ID |
| `corp_secret` | 应用密钥 |
| `agent_id` | 应用 ID（默认 1000058） |
| `adc_web_url` | 工单详情页前缀 URL |

---

## 错误处理

| 场景 | 脚本行为 | BPM Service 行为 |
|------|---------|----------------|
| 无待处理 ticket | 不推送，exit code 1 | 不触发 |
| 用户无法匹配企微 userid | 该人跳过，继续推送其他人 | 打印 warn 日志 |
| Webhook 返回非 2xx | 打印错误，exit code 2 | 打印 error 日志，不重试 |
| Webhook 返回 errcode != 0 | 打印 WeCom 错误码 | 打印 warn 日志 |
| 仓库无对应 robot URL | 跳过，打印 warn | `log.error` 记录，不发送 |
| DB 连接失败 | 抛出异常 | 打印 error，异步任务失败不影响主流程 |

---

## 已知未映射账号

以下账号在 `yami_devops.wecom_user_mapping` 中暂无记录，消息中不会 @ 到这些人：

| BPM 账号 | 问题 |
|----------|------|
| `haiwei.shen` | 不在 mapping 表 |
| `Mona` | 只有名字，无法精确匹配 |
| `ZICHEN.ZHAO` | 不在 mapping 表 |

**修复方式：** 在 `yami_devops.wecom_user_mapping` 中 INSERT 对应行，下次运行自动生效，无需改代码。
