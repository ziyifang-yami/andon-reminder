"""Debug two missing tickets"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(r"c:\DEV\wecom-bot-kiro-cli\workspace\skills")))
import pandas as pd
from sqlalchemy import text
from config.settings import engine

TICKETS = ("ADC2608280005", "ADC2609080007")

with engine.connect() as conn:
    # 1. 工单基本信息
    r = conn.execute(text("""
        SELECT ticket_no, status, business_type, pm_id, is_deleted,
               issue_desc, in_dtm
        FROM yamibuy_wh.wh_bpm_andon_cord
        WHERE ticket_no IN :t
    """), {"t": TICKETS})
    df = pd.DataFrame(r.fetchall(), columns=r.keys())
    print("=== 工单基本信息 ===")
    for _, row in df.iterrows():
        print(dict(row))

    # 2. processor 信息
    r2 = conn.execute(text("""
        SELECT p.ticket_no, p.user_id, p.symbol, u.email, u.user_name
        FROM yamibuy_wh.wh_bpm_processor p
        LEFT JOIN yamibuy_master.xysc_admin_user u ON u.user_id = p.user_id
        WHERE p.ticket_no IN :t
    """), {"t": TICKETS})
    df2 = pd.DataFrame(r2.fetchall(), columns=r2.keys())
    print("\n=== Processor 记录 ===")
    for _, row in df2.iterrows():
        print(dict(row))

    # 3. pm_id 对应的 email 和 user_mapping
    r3 = conn.execute(text("""
        SELECT a.ticket_no, a.pm_id, u.email, u.user_name,
               m.wecom_userid, m.wecom_department, m.wecom_position, m.is_excluded
        FROM yamibuy_wh.wh_bpm_andon_cord a
        LEFT JOIN yamibuy_master.xysc_admin_user u ON u.user_id = a.pm_id
        LEFT JOIN yami_devops.user_mapping m ON LOWER(TRIM(u.email)) = LOWER(TRIM(m.google_email))
        WHERE a.ticket_no IN :t
    """), {"t": TICKETS})
    df3 = pd.DataFrame(r3.fetchall(), columns=r3.keys())
    print("\n=== pm_id → user_mapping ===")
    for _, row in df3.iterrows():
        print(dict(row))

    # 4. 是否被 Block1 的 single-processor 过滤掉
    r4 = conn.execute(text("""
        SELECT ticket_no, COUNT(DISTINCT user_id) as proc_count
        FROM yamibuy_wh.wh_bpm_processor
        WHERE symbol != 3 AND user_id NOT IN (26, 4355, 8864)
          AND ticket_no IN :t
        GROUP BY ticket_no
    """), {"t": TICKETS})
    df4 = pd.DataFrame(r4.fetchall(), columns=r4.keys())
    print("\n=== processor 数量（符合条件）===")
    print(df4)
