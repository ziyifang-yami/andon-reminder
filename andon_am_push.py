"""Andon Cord AM (Account Management) Pending Ticket Push -> WeCom Webhook

AM 人员判断逻辑：
  - 企微部门 IN ('Account Management', 'Account Managment')  ← 两种拼写都存在

Usage:
    python andon_am_push.py
    python andon_am_push.py --dry-run
"""

import os
import sys
import argparse
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

SKILLS_DIR = Path(
    os.getenv(
        "SKILLS_DIR",
        "/opt/wecom-bot-kiro-cli/workspace/skills"
        if Path("/opt/wecom-bot-kiro-cli/workspace/skills").exists()
        else r"c:\DEV\wecom-bot-kiro-cli\workspace\skills",
    )
)
sys.path.insert(0, str(SKILLS_DIR))

import pandas as pd
from sqlalchemy import text
from config.settings import engine

LA = ZoneInfo("America/Los_Angeles")

WEBHOOK_URL = os.getenv(
    "ANDON_WEBHOOK_URL",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=eb30519a-6e37-4e74-972c-e5bc4ffbc890",
)

# 排除的系统账号 user_id（IT / 测试 / 系统机器人）
EXCLUDED_UIDS = (26, 4355, 8864)

# AM 所属企微部门（两种拼写都存在）
AM_DEPARTMENTS = ("Account Management", "Account Managment")


# ── 1. Query ──────────────────────────────────────────────────────────────────

def _build_sql() -> str:
    excl    = ",".join(str(u) for u in EXCLUDED_UIDS)
    am_ph   = ",".join(f"'{d}'" for d in AM_DEPARTMENTS)

    scope = f"""AND m.is_excluded = 0
    AND m.wecom_department IN ({am_ph})"""

    log_join = """LEFT JOIN (
    SELECT l1.ticket_no, l1.in_dtm AS last_log_dtm
    FROM yamibuy_wh.wh_bpm_andon_cord_log l1
    INNER JOIN (
        SELECT ticket_no, MAX(rec_id) AS max_id
        FROM yamibuy_wh.wh_bpm_andon_cord_log
        GROUP BY ticket_no
    ) l2 ON l1.rec_id = l2.max_id
) last_log ON a.ticket_no = last_log.ticket_no"""

    ticket_filter = """WHERE a.status = 4 AND a.is_deleted = 0
  AND a.issue_desc NOT LIKE '%%test%%'
  AND a.issue_desc NOT LIKE '%%\u6d4b\u8bd5%%'"""

    dept_expr = "TRIM(SUBSTRING_INDEX(wum.department, '/', -1))"
    area_expr = "TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(wum.department, '/', 2), '/', -1))"

    cols = f"""
    p_uid AS user_id, m.wecom_userid,
    COALESCE({dept_expr}, m.wecom_department) AS display_dept,
    COALESCE({area_expr}, 'Unknown') AS area,
    m.wecom_position, m.wecom_name,
    a.ticket_no, a.in_dtm, last_log.last_log_dtm"""

    # Block 1: 单处理人 ticket，通过 wh_bpm_processor 匹配
    b1 = f"""
SELECT {cols}
FROM yamibuy_wh.wh_bpm_andon_cord a
INNER JOIN (
    SELECT ticket_no FROM yamibuy_wh.wh_bpm_processor
    WHERE symbol != 3 AND user_id NOT IN ({excl})
    GROUP BY ticket_no HAVING COUNT(DISTINCT user_id) = 1
) single ON a.ticket_no = single.ticket_no
INNER JOIN yamibuy_wh.wh_bpm_processor proc
    ON a.ticket_no = proc.ticket_no AND proc.symbol != 3 AND proc.user_id NOT IN ({excl})
LEFT JOIN (SELECT user_id AS p_uid, email AS p_email FROM yamibuy_master.xysc_admin_user) u
    ON proc.user_id = u.p_uid
INNER JOIN yami_devops.user_mapping m ON LOWER(TRIM(u.p_email)) = LOWER(TRIM(m.google_email)) {scope}
LEFT JOIN yami_devops.wecom_user_mapping wum ON m.wecom_userid = wum.userid
{log_join}
{ticket_filter}"""

    # Block 2: pm_id 指向 AM 人员的 ticket（多处理人或无 processor 记录的情况）
    b2 = f"""
SELECT {cols}
FROM yamibuy_wh.wh_bpm_andon_cord a
LEFT JOIN (SELECT user_id AS p_uid, email AS p_email FROM yamibuy_master.xysc_admin_user) u_am
    ON a.pm_id = u_am.p_uid
INNER JOIN yami_devops.user_mapping m ON LOWER(TRIM(u_am.p_email)) = LOWER(TRIM(m.google_email)) {scope}
LEFT JOIN yami_devops.wecom_user_mapping wum ON m.wecom_userid = wum.userid
{log_join}
{ticket_filter}
  AND a.pm_id > 0
  AND a.ticket_no NOT IN (
      SELECT ticket_no FROM yamibuy_wh.wh_bpm_processor
      WHERE symbol != 3 AND user_id NOT IN ({excl})
      GROUP BY ticket_no HAVING COUNT(DISTINCT user_id) = 1
  )"""

    return b1 + "\nUNION ALL\n" + b2 + "\nORDER BY area, display_dept, user_id, in_dtm ASC\n"


def query_pending() -> pd.DataFrame:
    with engine.connect() as conn:
        r = conn.execute(text(_build_sql()))
        return pd.DataFrame(r.fetchall(), columns=r.keys())


# ── 2. Build message ──────────────────────────────────────────────────────────

def since_last(in_dtm) -> str:
    if pd.isna(in_dtm):
        return "unknown"
    now_dt = datetime.now(LA)
    dt = pd.to_datetime(in_dtm, unit="s", utc=True).tz_convert(LA)
    days = (now_dt - dt).total_seconds() / 86400
    if days < 1:
        return f"{int(days * 24)}h ago"
    return f"{days:.0f}d ago"


def build_message(df: pd.DataFrame):
    now_str = datetime.now(LA).strftime("%Y-%m-%d %H:%M")
    mentioned: list[str] = []

    if df.empty:
        return None, []

    total_tickets = df["ticket_no"].nunique()
    lines = [
        f"**📋 AndonCord Pending Reminder  {now_str} PT**",
        "",
    ]    # 按 display_dept → user_id 分组
    df = df.copy()
    df = df.sort_values(["display_dept", "user_id", "in_dtm"])

    for dept, dept_grp in df.groupby("display_dept", sort=False):
        dept_ticket_cnt = dept_grp["ticket_no"].nunique()
        lines.append(f"** {dept}  ({dept_ticket_cnt})**")

        for _, person_grp in dept_grp.groupby("user_id", sort=False):
            wecom_uid  = person_grp.iloc[0]["wecom_userid"]
            ticket_cnt = len(person_grp)
            lines.append(f"<@{wecom_uid}>  ({ticket_cnt})")
            if wecom_uid not in mentioned:
                mentioned.append(wecom_uid)

            for _, row in person_grp.sort_values("in_dtm").iterrows():
                ago    = since_last(row["in_dtm"])
                ticket = row["ticket_no"]
                lines.append(f"    {ticket}  created: {ago}")

        lines.append("")

    return "\n".join(lines), mentioned


# ── 3. Webhook POST ───────────────────────────────────────────────────────────

def push_webhook(webhook_url: str, content: str, mentioned_list: list) -> bool:
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        errcode = data.get("errcode", 0)
        if errcode != 0:
            print(f"[ERROR] errcode={errcode} errmsg={data.get('errmsg')}")
            return False
        print(f"[OK] pushed -- {len(mentioned_list)} @mentions: {mentioned_list}")
        return True
    except Exception as e:
        print(f"[ERROR] push failed: {e}")
        return False


# ── 4. Entry point ────────────────────────────────────────────────────────────

def run(webhook_url: str = WEBHOOK_URL, dry_run: bool = False) -> int:
    print(f"[am-push] querying... {datetime.now(LA).strftime('%Y-%m-%d %H:%M PT')}")

    df = query_pending()
    total = df["ticket_no"].nunique()
    print(f"[am-push] {total} pending AM ticket(s)")

    if df.empty:
        print("[am-push] no pending tickets, skip")
        return 1

    content, mentioned = build_message(df)

    if not content or not mentioned:
        print("[am-push] no mentionable AM processors, skip")
        return 1

    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)
    print(f"mentioned ({len(mentioned)}): {mentioned}\n")

    if dry_run:
        print("[am-push] dry-run mode — skipping webhook POST")
        return 0

    return 0 if push_webhook(webhook_url, content, mentioned) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Andon Cord AM push")
    parser.add_argument("--dry-run", action="store_true", help="Print message without posting to webhook")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))
