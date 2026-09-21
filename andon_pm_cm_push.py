"""Andon Cord PM/CM Pending Ticket Push -> WeCom Webhook

Usage:
    python andon_pm_cm_push.py
    python andon_pm_cm_push.py --group us-west
    python andon_pm_cm_push.py --group us-east
    python andon_pm_cm_push.py --group apac
"""

import os
import sys
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
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=73c96daf-7b11-4da8-97e8-a17bf211814e",
)

EXCLUDED_UIDS = (26, 4355, 8864)

AREA_DISPLAY = {
    "\u6d1b\u6749\u77f6\uff0c\u52a0\u5dde": "LA",
    "\u7231\u8fea\u751f, \u65b0\u6cfd\u897f": "NJ",
    "\u4e1c\u4eac\u5206\u90e8": "Tokyo",
    "\u97e9\u56fd\u5206\u90e8": "Korea",
    "Xi'an": "Xi'an",
    "Unknown": "Unknown",
}

AREA_EMOJI = {
    "LA":      "\U0001f334",
    "NJ":      "\U0001f3d9\ufe0f",
    "Tokyo":   "\U0001f5fc",
    "Korea":   "\U0001f1f0\U0001f1f7",
    "Xi'an":   "\U0001f3ef",
    "Canada":  "\U0001f341",
    "Unknown": "\u2753",
}

DEPT_DISPLAY = {
    "\u4e1c\u4eac\u5206\u90e8": "Tokyo Purchasing",
    "\u91c7\u8d2d\u90e8":       "NJ Purchasing",
    "\u97e9\u56fd\u5206\u90e8": "Korea Purchasing",
}

USERID_AREA_OVERRIDE = {
    "xiaolinchen":   "Canada",
    "hongbangzhang": "Canada",
    "sashamao":      "Canada",
}

PUSH_GROUPS = {
    "us-west": ["LA"],
    "us-east": ["NJ", "Canada"],
    "apac":    ["Tokyo", "Korea", "Xi'an"],
}

PM_DEPTS = (
    "Food Procurement",
    "Non Food Procurement",
    "Japan & Korea",
    "Procurement Management",
    "Procurement Management-China Direct",
    "Strategic Supply Solutions",
    "\u4e1c\u4eac\u5206\u90e8",
    "\u91c7\u8d2d\u90e8",
    "\u97e9\u56fd\u5206\u90e8",
)
CM_DEPTS = (
    "Food Category Growth Team",
    "Non Food Category Growth Team",
)
ALL_DEPTS = PM_DEPTS + CM_DEPTS

CM_POSITION_KEYWORDS = ("CATEGORY", "CM LEAD", "CM Lead")
PM_POSITION_KEYWORDS = (
    "PURCHASING", "PROCUREMENT", "PM Lead", "PM LEAD",
    "SUPPLY CHAIN", "Korean Brands", "DIRECTOR, STRATEGIC", "\u91c7\u8d2d",
    "PM Specialist", "Sr. PM",
)
POSITION_EXCLUDE = ("Intern", "Engineer", "Developer", "Software", "Java", "QA")

CM_EXTRA_USERIDS: tuple = ("ChengYan",)
PM_EXTRA_USERIDS = ("ChaiYanHua", "Julie.Zhu")


# ── 1. Query ──────────────────────────────────────────────────────────────────

def _build_sql() -> str:
    excl     = ",".join(str(u) for u in EXCLUDED_UIDS)
    all_ph   = ",".join(f"'{d}'" for d in ALL_DEPTS)
    pm_ex_ph = ",".join(f"'{u}'" for u in PM_EXTRA_USERIDS)
    cm_ex_ph = ",".join(f"'{u}'" for u in CM_EXTRA_USERIDS) if CM_EXTRA_USERIDS else "'__none__'"

    cm_pos   = " OR ".join(f"UPPER(m.wecom_position) LIKE UPPER('%{k}%')" for k in CM_POSITION_KEYWORDS)
    pm_pos   = " OR ".join(f"UPPER(m.wecom_position) LIKE UPPER('%{k}%')" for k in PM_POSITION_KEYWORDS)
    excl_pos = " AND ".join(f"UPPER(m.wecom_position) NOT LIKE UPPER('%{k}%')" for k in POSITION_EXCLUDE)

    role = f"""CASE
        WHEN m.wecom_userid IN ({cm_ex_ph}) THEN 'CM'
        WHEN m.wecom_position IS NOT NULL AND TRIM(m.wecom_position) != '' AND ({cm_pos}) THEN 'CM'
        ELSE 'PM'
    END"""

    scope = f"""AND m.is_excluded = 0
    AND (
        (
            m.wecom_department IN ({all_ph})
            AND (({cm_pos}) OR ({pm_pos}) OR m.wecom_position IS NULL OR TRIM(m.wecom_position) = '')
            AND (m.wecom_position IS NULL OR TRIM(m.wecom_position) = '' OR ({excl_pos}))
        )
        OR m.wecom_userid IN ({pm_ex_ph})
        OR m.wecom_userid IN ({cm_ex_ph})
    )"""

    log_join = """LEFT JOIN (
    SELECT l1.ticket_no, l1.in_dtm AS last_log_dtm
    FROM yamibuy_wh.wh_bpm_andon_cord_log l1
    INNER JOIN (
        SELECT ticket_no, MAX(rec_id) AS max_id
        FROM yamibuy_wh.wh_bpm_andon_cord_log
        GROUP BY ticket_no
    ) l2 ON l1.rec_id = l2.max_id
) last_log ON a.ticket_no = last_log.ticket_no"""

    ticket_filter = """WHERE a.status = 4 AND a.business_type = 1 AND a.is_deleted = 0
  AND a.issue_desc NOT LIKE '%%test%%'
  AND a.issue_desc NOT LIKE '%%\u6d4b\u8bd5%%'"""

    dept_expr = "TRIM(SUBSTRING_INDEX(wum.department, '/', -1))"
    area_expr = "TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(wum.department, '/', 2), '/', -1))"

    cols = f"""
    p_uid AS user_id, m.wecom_userid,
    COALESCE({dept_expr}, m.wecom_department) AS display_dept,
    COALESCE({area_expr}, 'Unknown') AS area,
    m.wecom_position, {role} AS role,
    a.ticket_no, a.in_dtm, last_log.last_log_dtm"""

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

    b2 = f"""
SELECT {cols}
FROM yamibuy_wh.wh_bpm_andon_cord a
LEFT JOIN (SELECT user_id AS p_uid, email AS p_email FROM yamibuy_master.xysc_admin_user) u_pm
    ON a.pm_id = u_pm.p_uid
INNER JOIN yami_devops.user_mapping m ON LOWER(TRIM(u_pm.p_email)) = LOWER(TRIM(m.google_email)) {scope}
LEFT JOIN yami_devops.wecom_user_mapping wum ON m.wecom_userid = wum.userid
{log_join}
{ticket_filter}
  AND a.pm_id > 0
  AND a.ticket_no NOT IN (
      SELECT ticket_no FROM yamibuy_wh.wh_bpm_processor
      WHERE symbol != 3 AND user_id NOT IN ({excl})
      GROUP BY ticket_no HAVING COUNT(DISTINCT user_id) = 1
  )"""

    return b1 + "\nUNION ALL\n" + b2 + "\nORDER BY role, area, display_dept, user_id, in_dtm ASC\n"


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


def build_message(df: pd.DataFrame, target_areas: list[str] | None = None):
    now_str = datetime.now(LA).strftime("%Y-%m-%d %H:%M")
    mentioned: list[str] = []

    if df.empty:
        return None, []

    pm_df = df[df["role"] == "PM"]
    cm_df = df[df["role"] == "CM"]

    lines = [
        f"**Andon Cord Pending Reminder  {now_str} PT**",
        "",
    ]

    def render_group(grp_df: pd.DataFrame, label: str):
        if grp_df.empty:
            return
        grp_df = grp_df.copy()
        grp_df["area_display"] = grp_df.apply(
            lambda r: USERID_AREA_OVERRIDE.get(
                str(r["wecom_userid"]),
                AREA_DISPLAY.get(str(r["area"]), str(r["area"]))
            ), axis=1
        )
        if target_areas:
            grp_df = grp_df[grp_df["area_display"].isin(target_areas)]
            if grp_df.empty:
                return

        area_order = ["LA", "NJ", "Canada", "Tokyo", "Korea", "Xi'an", "Unknown"]
        grp_df["area_rank"] = grp_df["area_display"].map(
            lambda x: area_order.index(x) if x in area_order else 99
        )
        grp_df = grp_df.sort_values(["area_rank", "display_dept", "user_id", "in_dtm"])

        for area_disp, area_grp in grp_df.groupby("area_display", sort=False):
            emoji = AREA_EMOJI.get(area_disp, "\U0001f4cd")
            lines.append(f"**{emoji} {area_disp}**")

            for dept, dept_grp in area_grp.groupby("display_dept", sort=False):
                lines.append(f"**{DEPT_DISPLAY.get(dept, dept)}**")

                for _, person_grp in dept_grp.groupby("user_id", sort=False):
                    wecom_uid = person_grp.iloc[0]["wecom_userid"]
                    lines.append(f"<@{wecom_uid}>")
                    if wecom_uid not in mentioned:
                        mentioned.append(wecom_uid)
                    for _, row in person_grp.sort_values("in_dtm").iterrows():
                        ago = since_last(row["in_dtm"])
                        ticket = row["ticket_no"]
                        lines.append(f"    {ticket}  created: {ago}")

            lines.append("")

    render_group(pm_df, "PM")
    render_group(cm_df, "CM")

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

def run(webhook_url: str = WEBHOOK_URL, group: str | None = None) -> int:
    target_areas = PUSH_GROUPS.get(group) if group else None
    group_label  = group or "all"
    print(f"[pm-cm-push] querying... {datetime.now(LA).strftime('%Y-%m-%d %H:%M PT')}  group={group_label}")

    df = query_pending()
    total  = df["ticket_no"].nunique()
    pm_cnt = df[df["role"] == "PM"]["ticket_no"].nunique()
    cm_cnt = df[df["role"] == "CM"]["ticket_no"].nunique()
    print(f"[pm-cm-push] {total} total tickets ({pm_cnt} PM / {cm_cnt} CM) -- pushing {group_label}")

    if df.empty:
        print("[pm-cm-push] no pending tickets, skip")
        return 1

    content, mentioned = build_message(df, target_areas=target_areas)

    if not content or not mentioned:
        print(f"[pm-cm-push] no tickets for group={group_label}, skip")
        return 1

    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)
    print(f"mentioned ({len(mentioned)}): {mentioned}\n")

    return 0 if push_webhook(webhook_url, content, mentioned) else 2


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Andon Cord PM/CM push")
    parser.add_argument(
        "--group", choices=list(PUSH_GROUPS.keys()),
        help="Area group: us-west (LA), us-east (NJ+Canada), apac (Tokyo+Korea+Xi'an)"
    )
    args = parser.parse_args()
    sys.exit(run(group=args.group))
