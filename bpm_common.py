"""BPM 共享模块 — 部门映射、processor 查询等公共逻辑。
复制自 workspace/skills/bpm-ticket/scripts/bpm_common.py，不做改动。
"""
import sys
from pathlib import Path
# 让 settings.py 能找到
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent /
                        "DEV" / "wecom-bot-kiro-cli" / "workspace" / "skills"))

import pandas as pd
from config.settings import engine  # noqa: F401 (re-exported for importers)

# 仓库映射
WAREHOUSE_MAP = {"001": "LA", "002": "NJ"}

# 部门名统一映射
DEPT_MAP = {
    "BU-BEAUTY": "PM", "BU-FOOD": "PM", "BU-HEALTH": "PM",
    "BU-HOME": "PM", "BU-SNACK": "PM", "BU-BABY": "PM",
    "BU_LEAD": "PM", "BU_1P": "PM", "Food": "PM",
    "Marketplace": "AM", "MKPL": "AM",
    "BSA": "PM", "Business System Analysis": "PM",
    "Warehouse": "FC", "WH": "FC",
    "DESGIN": "Design",
}

# 手动部门覆盖
USER_DEPT_FALLBACK = {
    "irene.yu": "AM", "Xina.Qu": "AM", "Joeyi.Cao": "AM",
    "Celia.Liang": "AM",
    "kira.chen": "CM",
    "aiden.duan": "FC", "wenbo.ban": "ICQA",
    "sally.jia": "ICQA",
    "flora.feng": "AM",
}

# 系统/非业务账号
EXCLUDED_UIDS = {26}  # little (IT)

# Status 映射
ANDON_STATUS_MAP = {
    1: "待WH初审", 2: "待Supervisor处理", 3: "待确认",
    4: "待BU处理", 5: "待审批", 6: "跨部门处理中",
}


def clean_dept(dept, account=None):
    """统一部门名称。"""
    if account and account in USER_DEPT_FALLBACK:
        return USER_DEPT_FALLBACK[account]
    if pd.isna(dept) or str(dept).strip() == "":
        return "Unknown"
    return DEPT_MAP.get(str(dept).strip(), str(dept).strip())


def map_warehouse(code):
    """仓库编号转名称。"""
    if pd.isna(code) or str(code).strip() == "":
        return "Unknown"
    return WAREHOUSE_MAP.get(str(code).strip(), str(code).strip())
