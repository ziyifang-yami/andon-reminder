"""Andon Cord 待处理任务 → 企微 Webhook 精准推送

数据关联路径（SQL 层完成，无 Python 模糊匹配）：
  wh_bpm_andon_cord (待处理 ticket)
    ↓ ticket_no
  wh_bpm_processor (处理人分配, symbol != 3, user_id NOT IN excluded)
    ↓ user_id
  xysc_admin_user (email)
    ↓ LOWER(email) = LOWER(email)
  wecom_user_mapping (userid → 企微 @)

匹配不上的人 = 离职或测试账号，直接忽略，不推送。

用法：
    python andon_wecom_push.py
    ANDON_WEBHOOK_URL=https://... python andon_wecom_push.py
"""

import os
import sys
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

SKILLS_DIR = Path(r"c:\DEV\wecom-bot-kiro-cli\workspace\skills")
BPM_SCRIPTS = SKILLS_DIR / "bpm-ticket" / "scripts"
sys.path.insert(0, str(SKILLS_DIR))
sys.path.insert(0, str(BPM_SCRIPTS))

import pandas as pd
from sqlalchemy import text
from config.settings import engine

LA = ZoneInfo("America/Los_Angeles")

WEBHOOK_URL = os.getenv(
    "ANDON_WEBHOOK_URL",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=4de640f1-4626-42c9-9358-eac850dc5967"
)

# 排除的系统账号 user_id（IT / 测试 / 系统机器人）
EXCLUDED_UIDS = (26,)


# ── 1. 核心查询：一条 SQL 拿到所有需要的数据 ─────────────────────────────────

PENDING_SQL = """
SELECT
    p.user_id,
    p.processor                                          AS bpm_name,
    COALESCE(NULLIF(u.department, ''), ps.department)    AS raw_dept,
    u.user_name                                          AS account,
    u.email                                              AS admin_email,
    COALESCE(w1.userid,       w2.userid,       w3.userid)       AS wecom_userid,
    COALESCE(w1.display_name, w2.display_name, w3.display_name) AS wecom_name,
    a.ticket_no,
    a.in_dtm,
    last_log.last_log_dtm
FROM yamibuy_wh.wh_bpm_andon_cord a
INNER JOIN yamibuy_wh.wh_bpm_processor p
    ON  a.ticket_no = p.ticket_no
    AND p.symbol    != 3
    AND p.user_id   NOT IN ({excluded})
-- 只取单处理人 ticket（业务处理人数 = 1）
INNER JOIN (
    SELECT ticket_no
    FROM yamibuy_wh.wh_bpm_processor
    WHERE symbol  != 3
      AND user_id NOT IN ({excluded})
    GROUP BY ticket_no
    HAVING COUNT(DISTINCT user_id) = 1
) single ON a.ticket_no = single.ticket_no
LEFT JOIN yamibuy_master.xysc_admin_user u
    ON p.user_id = u.user_id
LEFT JOIN (
    SELECT user_id, department
    FROM yamibuy_wh.wh_bpm_processor_setting
    GROUP BY user_id, department
) ps ON p.user_id = ps.user_id
-- 取每个 ticket 最后一条 log 的时间
LEFT JOIN (
    SELECT l1.ticket_no, l1.in_dtm AS last_log_dtm
    FROM yamibuy_wh.wh_bpm_andon_cord_log l1
    INNER JOIN (
        SELECT ticket_no, MAX(rec_id) AS max_id
        FROM yamibuy_wh.wh_bpm_andon_cord_log
        GROUP BY ticket_no
    ) l2 ON l1.rec_id = l2.max_id
) last_log ON a.ticket_no = last_log.ticket_no
-- Level 1: email 精确匹配
LEFT JOIN yami_devops.wecom_user_mapping w1
    ON LOWER(TRIM(u.email)) = LOWER(TRIM(w1.email))
-- Level 2: display_name 模糊匹配
LEFT JOIN yami_devops.wecom_user_mapping w2
    ON w1.userid IS NULL
    AND LOWER(REPLACE(u.user_name, '.', ' ')) = LOWER(w2.display_name)
-- Level 3: userid 模糊匹配
LEFT JOIN yami_devops.wecom_user_mapping w3
    ON w1.userid IS NULL AND w2.userid IS NULL
    AND LOWER(REPLACE(u.user_name, '.', '')) = LOWER(w3.userid)
WHERE a.status     != 7
  AND a.is_deleted  = 0
  AND a.issue_desc NOT LIKE '%%测试%%'
  AND a.issue_desc NOT LIKE '%%test%%'
  AND COALESCE(w1.userid, w2.userid, w3.userid) IS NOT NULL
ORDER BY p.user_id, a.in_dtm ASC
"""

# 多处理人 ticket（用于消息末尾列出）
MULTI_SQL = """
SELECT
    a.ticket_no,
    a.status,
    a.in_dtm,
    a.issue_desc,
    last_log.memo AS last_memo
FROM yamibuy_wh.wh_bpm_andon_cord a
LEFT JOIN (
    SELECT l1.ticket_no, l1.memo
    FROM yamibuy_wh.wh_bpm_andon_cord_log l1
    INNER JOIN (
        SELECT ticket_no, MAX(rec_id) AS max_id
        FROM yamibuy_wh.wh_bpm_andon_cord_log
        GROUP BY ticket_no
    ) l2 ON l1.rec_id = l2.max_id
) last_log ON a.ticket_no = last_log.ticket_no
WHERE a.status     != 7
  AND a.is_deleted  = 0
  AND a.issue_desc NOT LIKE '%%测试%%'
  AND a.issue_desc NOT LIKE '%%test%%'
  AND a.ticket_no IN (
      SELECT ticket_no
      FROM yamibuy_wh.wh_bpm_processor
      WHERE symbol  != 3
        AND user_id NOT IN ({excluded})
      GROUP BY ticket_no
      HAVING COUNT(DISTINCT user_id) != 1
  )
ORDER BY a.in_dtm ASC
"""


def qs(sql):
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return pd.DataFrame(result.fetchall(), columns=result.keys())


# ── 2. 部门归一化 ────────────────────────────────────────────────────────────

DEPT_MAP = {
    "BU-BEAUTY": "PM", "BU-FOOD": "PM", "BU-HEALTH": "PM",
    "BU-HOME": "PM",   "BU-SNACK": "PM", "BU-BABY": "PM",
    "BU_LEAD": "PM",   "BU_1P": "PM",    "Food": "PM",
    "BSA": "PM",       "Business System Analysis": "PM",
    "Marketplace": "AM", "MKPL": "AM",
    "Warehouse": "FC", "WH": "FC",
    "DESGIN": "Design",
}
USER_DEPT_FALLBACK = {
    "irene.yu": "AM", "Xina.Qu": "AM", "Joeyi.Cao": "AM", "Celia.Liang": "AM",
    "kira.chen": "CM", "aiden.duan": "FC", "wenbo.ban": "ICQA",
    "sally.jia": "ICQA", "flora.feng": "AM",
}

def clean_dept(raw_dept, account):
    if account in USER_DEPT_FALLBACK:
        return USER_DEPT_FALLBACK[account]
    if not raw_dept or str(raw_dept).strip() == "" or str(raw_dept) == "nan":
        return "Unknown"
    return DEPT_MAP.get(str(raw_dept).strip(), str(raw_dept).strip())


def get_status_label(status, memo):
    """从 log memo 提取真实状态文字。"""
    import re
    if memo:
        m = re.search(r'状态更新为[:：]([^,，\n]+)', str(memo))
        if m:
            return m.group(1).strip()
    STATUS_MAP = {
        1: "待WH初审", 2: "待Supervisor", 3: "待确认",
        4: "待BU处理", 5: "待审批", 6: "跨部门处理中",
    }
    return STATUS_MAP.get(status, f"状态{status}")


# ── 3. 构建消息 ──────────────────────────────────────────────────────────────

def build_message(single_df, multi_df, total):
    now_str = datetime.now(LA).strftime("%Y-%m-%d %H:%M")
    TARGET_DEPTS = {"AM", "PM", "CM"}
    mentioned = []

    if single_df.empty:
        return f"📋 Andon Cord 待处理提醒  {now_str} PT\nAM/PM/CM 暂无待处理 Ticket", []

    now_dt = datetime.now(LA)

    # 归一化部门，只保留目标部门
    single_df = single_df.copy()
    single_df["dept"] = single_df.apply(
        lambda r: clean_dept(r["raw_dept"], r["account"] or ""), axis=1
    )
    single_df = single_df[single_df["dept"].isin(TARGET_DEPTS)]

    if single_df.empty:
        return f"📋 Andon Cord 待处理提醒  {now_str} PT\nAM/PM/CM 暂无待处理 Ticket", []

    # 计算距上次处理时间（天）
    def since_last(last_log_dtm):
        if pd.isna(last_log_dtm):
            return "未处理"
        dt = pd.to_datetime(last_log_dtm, unit="s", utc=True).tz_convert(LA)
        days = (now_dt - dt).total_seconds() / 86400
        if days < 1:
            hours = int(days * 24)
            return f"{hours}h前"
        return f"{days:.0f}天前"

    total_tickets = single_df["ticket_no"].nunique()

    lines = [
        f"📋 Andon Cord 待处理提醒  {now_str} PT",
        f"AM/PM/CM 共 {total_tickets} 条待处理 Ticket，请及时跟进 👇",
        "",
    ]

    # 按部门 → 人分组
    dept_totals = single_df.groupby("dept")["ticket_no"].count()
    cm_depts = [d for d in dept_totals.index if d == "CM"]
    non_cm   = sorted([d for d in dept_totals.index if d != "CM"],
                      key=lambda d: dept_totals[d], reverse=True)
    dept_order = {d: i for i, d in enumerate(non_cm + cm_depts)}
    single_df["dept_rank"] = single_df["dept"].map(dept_order).fillna(99)
    single_df = single_df.sort_values(["dept_rank", "user_id", "in_dtm"])

    for dept, dept_grp in single_df.groupby("dept", sort=False):
        dept_ticket_cnt = dept_grp["ticket_no"].nunique()
        lines.append(f"▌{dept}  ({dept_ticket_cnt} 条)")

        for uid_val, person_grp in dept_grp.groupby("user_id", sort=False):
            wecom_uid = person_grp.iloc[0]["wecom_userid"]
            ticket_cnt = len(person_grp)

            # mentioned_list 触发 @ 通知，content 里只写名字不加 @
            wecom_name = person_grp.iloc[0]["wecom_name"] or wecom_uid
            lines.append(f"  {wecom_name}  ({ticket_cnt} 条)")
            if wecom_uid not in mentioned:
                mentioned.append(wecom_uid)

            # 列出每条 ticket：ADC号 + 距上次处理时间
            for _, row in person_grp.iterrows():
                ago = since_last(row["last_log_dtm"])
                lines.append(f"    • {row['ticket_no']}  上次处理: {ago}")

        lines.append("")

    return "\n".join(lines), mentioned


# ── 4. Webhook POST ──────────────────────────────────────────────────────────

def push_webhook(url, content, mentioned_list):
    payload = {
        "msgtype": "text",
        "text": {
            "content": content,
            "mentioned_list": mentioned_list,
        }
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        errcode = data.get("errcode", 0)
        if errcode != 0:
            print(f"[ERROR] WeCom errcode={errcode} errmsg={data.get('errmsg')}")
            return False
        print(f"[OK] pushed — {len(mentioned_list)} @mentions")
        return True
    except Exception as e:
        print(f"[ERROR] push failed: {e}")
        return False


# ── 5. 主入口 ────────────────────────────────────────────────────────────────

def run(webhook_url=WEBHOOK_URL):
    excluded_str = ",".join(str(u) for u in EXCLUDED_UIDS)
    print(f"[andon-push] querying... {datetime.now(LA).strftime('%Y-%m-%d %H:%M PT')}")

    single_df = qs(PENDING_SQL.format(excluded=excluded_str))
    multi_df  = qs(MULTI_SQL.format(excluded=excluded_str))

    total = single_df["ticket_no"].nunique() + len(multi_df)

    if total == 0:
        print("[andon-push] no pending tickets, skip")
        return 1

    matched = single_df["user_id"].nunique()
    print(f"[andon-push] {matched} active processors with pending tickets (inactive/test users filtered by INNER JOIN)")

    content, mentioned = build_message(single_df, multi_df, total)

    # 预览
    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)
    print(f"mentioned_list ({len(mentioned)}): {mentioned}\n")

    if "YOUR_KEY_HERE" in webhook_url:
        print("[WARN] webhook URL not set — dry run only")
        return 0

    return 0 if push_webhook(webhook_url, content, mentioned) else 2


if __name__ == "__main__":
    sys.exit(run())
