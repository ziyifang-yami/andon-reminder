-- Andon Cord 任务表 schema
-- 文件路径: data/andon.db
--
-- 如果实际表名/字段名不同，同步修改 src/andon_reminder.rs 中的 SQL 查询。

CREATE TABLE IF NOT EXISTS andon_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_title       TEXT    NOT NULL,
    assignee_userid  TEXT    NOT NULL,   -- 企微 userid，用于 @人
    status           TEXT    NOT NULL DEFAULT 'open',
                                          -- 'open' | 'resolved' | 'closed'
    created_at       TEXT    DEFAULT (datetime('now')),
    updated_at       TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_andon_tasks_status ON andon_tasks(status);

-- ── 测试数据（可选）────────────────────────────────────────────────────────
INSERT INTO andon_tasks (task_title, assignee_userid, status)
VALUES
    ('生产线 A 停机未恢复',   'zhangsan',  'open'),
    ('质量问题需复核',        'lisi',      'open'),
    ('原材料短缺待采购审批',  'wangwu',    'open'),
    ('已修复的任务（不推送）', 'testuser', 'resolved');
