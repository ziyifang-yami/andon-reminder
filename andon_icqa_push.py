"""Andon Cord ICQA/FC 待处理提醒

Andon Cord Status 枚举（主表 status 字段）：
  1=待CS处理  2=待FC处理  3=待ICQA处理
  4=待BU处理  5=待拍照    6=取消  7=已完成

按 status + 仓库映射到固定负责人：
  status=2 待FC处理   + 001(LA) → Jenny Yu
  status=2 待FC处理   + 002(NJ) → Xuezhen Li
  status=3 待ICQA处理 + 001(LA) → Ziyi Fang
  status=3 待ICQA处理 + 002(NJ) → Claire Zhao
"""

import sys
import os
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

SKILLS_DIR = Path(
    os.getenv("SKILLS_DIR",
              # 服务器路径
              "/opt/wecom-bot-kiro-cli/workspace/skills"
              if Path("/opt/wecom-bot-kiro-cli/workspace/skills").exists()
              # 本地 Windows 路径
              else r"c:\DEV\wecom-bot-kiro-cli\workspace\skills")
)
sys.path.insert(0, str(SKILLS_DIR))

import pandas as pd
from sqlalchemy import text
from config.settings import engine

LA = ZoneInfo("America/Los_Angeles")

WEBHOOK_URL = os.getenv(
    "ANDON_WEBHOOK_URL",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=4de640f1-4626-42c9-9358-eac850dc5967"
)

# key: (status_int, warehouse)  value: (wecom_userid, display_name, label)
ASSIGNEE_MAP = {
    (2, "001"): ("jenny.yu.dgxg@gmail.com", "Jenny Yu",    "待FC处理"),
    (2, "002"): ("xuezhenli",               "Xuezhen Li",  "待FC处理"),
    (3, "001"): ("Ziyi.fang@YAMIBUY.COM",   "Ziyi Fang",   "待ICQA处理"),
    (3, "002"): ("clairezhao",              "Claire Zhao", "待ICQA处理"),
}

PUSH_ORDER = [
    (3, "001"),  # ICQA LA → Ziyi
    (3, "002"),  # ICQA NJ → Claire
    (2, "001"),  # FC   LA → Jenny
    (2, "002"),  # FC   NJ → Xuezhen
]


# ── 查询 ─────────────────────────────────────────────────────────────────────

def query_pending() -> pd.DataFrame:
    """查 status=2(待FC处理) / status=3(待ICQA处理) 的 andon ticket。

    返回每条 ticket 的基本信息 + 处理人数量 + 单处理人信息（若唯一）。
    """
    sql = """
    SELECT
        a.ticket_no,
        a.warehouse_number,
        a.status,
        a.in_dtm,
        last_log.in_dtm                                           AS last_log_dtm,
        -- 业务处理人数（排除系统账号和已取消）
        COUNT(DISTINCT CASE
            WHEN p.symbol != 3 AND p.user_id NOT IN (26, 4355, 8864)
            THEN p.user_id END)                                   AS proc_cnt,
        -- 若唯一处理人，取其 user_id 和 processor 名
        MIN(CASE
            WHEN p.symbol != 3 AND p.user_id NOT IN (26, 4355, 8864)
            THEN p.user_id END)                                   AS sole_user_id,
        MIN(CASE
            WHEN p.symbol != 3 AND p.user_id NOT IN (26, 4355, 8864)
            THEN p.processor END)                                 AS sole_processor
    FROM yamibuy_wh.wh_bpm_andon_cord a
    LEFT JOIN yamibuy_wh.wh_bpm_processor p ON a.ticket_no = p.ticket_no
    LEFT JOIN (
        SELECT l1.ticket_no, l1.in_dtm
        FROM yamibuy_wh.wh_bpm_andon_cord_log l1
        INNER JOIN (
            SELECT ticket_no, MAX(rec_id) AS max_id
            FROM yamibuy_wh.wh_bpm_andon_cord_log
            GROUP BY ticket_no
        ) l2 ON l1.rec_id = l2.max_id
    ) last_log ON a.ticket_no = last_log.ticket_no
    WHERE a.status IN (2, 3)
      AND a.is_deleted = 0
      AND a.issue_desc NOT LIKE '%%测试%%'
      AND a.issue_desc NOT LIKE '%%test%%'
    GROUP BY a.ticket_no
    ORDER BY a.warehouse_number, a.in_dtm ASC
    """
    with engine.connect() as conn:
        r = conn.execute(text(sql))
        return pd.DataFrame(r.fetchall(), columns=r.keys())


def since_last(last_log_dtm) -> str:
    if pd.isna(last_log_dtm):
        return "未处理"
    now_dt = datetime.now(LA)
    dt = pd.to_datetime(last_log_dtm, unit="s", utc=True).tz_convert(LA)
    days = (now_dt - dt).total_seconds() / 86400
    if days < 1:
        return f"{int(days * 24)}h前"
    return f"{days:.0f}天前"


def load_wecom_mapping() -> dict:
    """加载 xysc_admin_user.user_id → wecom userid 的映射（通过 email join）。"""
    sql = """
    SELECT u.user_id, u.user_name, w.userid AS wecom_userid, w.display_name
    FROM yamibuy_master.xysc_admin_user u
    INNER JOIN yami_devops.wecom_user_mapping w
        ON LOWER(TRIM(u.email)) = LOWER(TRIM(w.email))
    WHERE w.userid IS NOT NULL
    """
    with engine.connect() as conn:
        r = conn.execute(text(sql))
        df = pd.DataFrame(r.fetchall(), columns=r.keys())
    # user_id → (wecom_userid, display_name)
    return {int(row.user_id): (row.wecom_userid, row.display_name)
            for _, row in df.iterrows()}


# ── 构建消息 ─────────────────────────────────────────────────────────────────

def build_message(df: pd.DataFrame):
    now_str = datetime.now(LA).strftime("%Y-%m-%d %H:%M")
    mentioned = []

    if df.empty:
        return None, []

    # 加载 user_id → 企微 userid 映射
    wecom_map = load_wecom_mapping()

    # 按 (status, warehouse) 分组，每组再细分单/多处理人
    # 结构: group_key → { 'single': [(wecom_uid, display, [tickets])],
    #                     'fallback': [tickets] }
    STATUS_LABEL = {2: "待FC处理", 3: "待ICQA处理"}
    groups: dict = {}

    for _, row in df.iterrows():
        key = (int(row["status"]), str(row["warehouse_number"]))
        if key not in ASSIGNEE_MAP:
            continue
        groups.setdefault(key, {"single": {}, "fallback": []})

        proc_cnt = int(row["proc_cnt"]) if pd.notna(row["proc_cnt"]) else 0
        sole_uid  = int(row["sole_user_id"]) if pd.notna(row["sole_user_id"]) else None

        if proc_cnt == 1 and sole_uid and sole_uid in wecom_map:
            # 单处理人且能匹配企微 userid → 直接 @ 本人
            wecom_uid, display = wecom_map[sole_uid]
            groups[key]["single"].setdefault(wecom_uid, {
                "display": display, "tickets": []
            })
            groups[key]["single"][wecom_uid]["tickets"].append(row)
        else:
            # 多处理人 / 无处理人 / 无法匹配 → 用默认负责人
            groups[key]["fallback"].append(row)

    if not groups:
        return None, []

    # 统计总票数
    total = len(df[df.apply(
        lambda r: (int(r["status"]), str(r["warehouse_number"])) in ASSIGNEE_MAP, axis=1
    )])

    lines = [
        f"🔔 **Andon Cord 待处理提醒**  {now_str} PT",
        f"共 **{total}** 条，请及时跟进",
        "",
    ]

    for key in PUSH_ORDER:
        if key not in groups:
            continue
        g = groups[key]
        status_int, wh = key
        wh_name = "LA" if wh == "001" else "NJ"
        label = STATUS_LABEL[status_int]
        fallback_uid, fallback_display, _ = ASSIGNEE_MAP[key]

        # 计算本组总票数
        single_cnt   = sum(len(p["tickets"]) for p in g["single"].values())
        fallback_cnt = len(g["fallback"])
        section_cnt  = single_cnt + fallback_cnt
        if section_cnt == 0:
            continue

        lines.append(f"**⏳ {label} · {wh_name}**  ({section_cnt} 条)")

        # 单处理人中，与 fallback 同人的合并到 fallback；其余单独输出
        for wecom_uid, info in g["single"].items():
            if wecom_uid == fallback_uid:
                for row in info["tickets"]:
                    g["fallback"].append(row)
            else:
                lines.append(f"<@{wecom_uid}>")
                if wecom_uid not in mentioned:
                    mentioned.append(wecom_uid)
                for row in sorted(info["tickets"], key=lambda r: r["in_dtm"]):
                    ago = since_last(row["last_log_dtm"])
                    lines.append(f"    — {row['ticket_no']}  {ago}")

        # 多处理人 / 无处理人 ticket：@ 默认负责人
        if g["fallback"]:
            lines.append(f"<@{fallback_uid}>")
            if fallback_uid not in mentioned:
                mentioned.append(fallback_uid)
            for row in sorted(g["fallback"], key=lambda r: r["in_dtm"]):
                ago = since_last(row["last_log_dtm"])
                lines.append(f"    — {row['ticket_no']}  {ago}")

        lines.append("")

    return "\n".join(lines), mentioned


# ── 推送 ─────────────────────────────────────────────────────────────────────

def push_webhook(url: str, content: str, mentioned_list: list) -> bool:
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content}
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode", 0) != 0:
        print(f"[ERROR] WeCom errcode={data['errcode']} {data.get('errmsg')}")
        return False
    print(f"[OK] pushed (markdown) — {len(mentioned_list)} @mentions: {mentioned_list}")
    return True


# ── 主入口 ───────────────────────────────────────────────────────────────────

def run():
    now = datetime.now(LA).strftime("%Y-%m-%d %H:%M PT")
    print(f"[icqa-push] querying... {now}")

    df = query_pending()
    print(f"[icqa-push] {len(df)} FC/ICQA pending tickets")
    if not df.empty:
        print(df.groupby(["status", "warehouse_number"]).size().to_string())

    content, mentioned = build_message(df)

    if not content:
        print("[icqa-push] no FC/ICQA pending tickets, skip")
        return

    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)
    print(f"mentioned_list: {mentioned}\n")

    push_webhook(WEBHOOK_URL, content, mentioned)


if __name__ == "__main__":
    run()
