-- 0002 异常复核生命周期：可重新打开的工单 + append-only 复核事件流
-- 背景：同一异常（乱序/缺章等）在简介改写、重新解析后会消失再出现。
-- 旧结构下重新解析会按章节行 id 重建工单，覆盖上一次处置依据且无法重新进入待处理。
-- 本变更：
--   1. chapter_issues 增加 occurrence_count / last_seen_at / updated_at；
--   2. 新增 issue_events：每次「发现 / 自动关闭 / 重新打开 / 人工处置」追加一行，永不更新删除；
--   3. issue_key 改为按单集 + 类型 + 行序/时间锚点的稳定自然键（应用层生成，DDL 只保证唯一）。
-- 演示环境由 SQLAlchemy create_all 自动建表，本文件供正式环境在线变更与评审。
--
-- 注意：旧库升级时历史工单保留原 issue_key（本脚本不改写），其历史事件已在下方回填；
-- 应用层启用稳定 key 后，同一位置异常若 key 不同会另开一张工单并从 detected 重新计数，
-- 旧工单与旧结论仍保留可查。演示环境为全新 create_all，不存在该差异。

ALTER TABLE chapter_issues
  ADD COLUMN occurrence_count INT NOT NULL DEFAULT 1 AFTER resolution_note,
  ADD COLUMN last_seen_at DATETIME NULL AFTER occurrence_count,
  ADD COLUMN updated_at DATETIME NULL AFTER created_at;

-- 存量工单补齐默认值（create_all 新环境不需要，正式库升级时执行）
UPDATE chapter_issues SET last_seen_at = created_at WHERE last_seen_at IS NULL;
UPDATE chapter_issues SET updated_at = created_at WHERE updated_at IS NULL;

-- 回填完成后与 ORM 模型对齐：两列改为 NOT NULL
ALTER TABLE chapter_issues
  MODIFY last_seen_at DATETIME NOT NULL,
  MODIFY updated_at DATETIME NOT NULL;

CREATE TABLE IF NOT EXISTS issue_events (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  issue_id    INT          NOT NULL,
  event_type  VARCHAR(16)  NOT NULL COMMENT 'detected/auto_closed/reopened/resolved',
  actor       VARCHAR(160) NOT NULL DEFAULT '',
  note        VARCHAR(255) NOT NULL DEFAULT '',
  detail      JSON NULL,
  created_at  DATETIME     NOT NULL,
  KEY ix_issue_events_issue (issue_id),
  KEY ix_issue_events_type (event_type),
  KEY ix_issue_events_created (created_at),
  CONSTRAINT fk_issue_events_issue FOREIGN KEY (issue_id) REFERENCES chapter_issues (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 存量 open 工单补一条「发现异常」事件，保证升级后复核经过不空缺
INSERT INTO issue_events (issue_id, event_type, actor, note, detail, created_at)
SELECT id, 'detected', 'validator', COALESCE(message, ''), JSON_OBJECT('kind', kind), created_at
FROM chapter_issues
WHERE status = 'open'
  AND NOT EXISTS (SELECT 1 FROM issue_events e WHERE e.issue_id = chapter_issues.id);

-- 存量已处置工单补「发现 → 人工/自动关闭」两条事件
INSERT INTO issue_events (issue_id, event_type, actor, note, detail, created_at)
SELECT id, 'detected', 'validator', COALESCE(message, ''), JSON_OBJECT('kind', kind), created_at
FROM chapter_issues
WHERE status = 'resolved'
  AND NOT EXISTS (SELECT 1 FROM issue_events e WHERE e.issue_id = chapter_issues.id);

INSERT INTO issue_events (issue_id, event_type, actor, note, detail, created_at)
SELECT id,
       CASE WHEN resolved_by = 'system:rescan' THEN 'auto_closed' ELSE 'resolved' END,
       COALESCE(resolved_by, ''),
       COALESCE(resolution_note, ''),
       JSON_OBJECT('kind', kind),
       COALESCE(resolved_at, updated_at, created_at)
FROM chapter_issues
WHERE status = 'resolved'
  AND NOT EXISTS (
    SELECT 1 FROM issue_events e
    WHERE e.issue_id = chapter_issues.id AND e.event_type IN ('auto_closed', 'resolved')
  );
