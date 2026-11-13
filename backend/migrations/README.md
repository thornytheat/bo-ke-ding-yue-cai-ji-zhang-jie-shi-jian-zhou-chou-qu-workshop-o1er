# migrations

演示项目使用 **SQLAlchemy 2.0 `Base.metadata.create_all`** 在 backend 启动时建表
（`src/config/app.py::_bootstrap_database`），这样 `docker compose up` 不需要额外的迁移步骤。

`0001_initial.sql` 是同一份结构的可审阅 DDL（MySQL 8.0），用途：

- 给 DBA 在正式环境先建表，再让应用连上去；
- 评审时快速核对表结构、索引与外键。

结构变更约定：

1. 改 `src/models/*.py`；
2. 同步改本目录新增 `000N_*.sql`（只增不改历史文件）；
3. 若已有环境需要在线变更，单独写 migration 并在 README 记录。

已记录的结构变更：

- `0002_issue_lifecycle_events.sql`：异常工单可重新打开（`occurrence_count` / `last_seen_at` /
  `updated_at`），新增 append-only 的 `issue_events` 事件流，保留每轮发现、自动关闭、
  重新打开与人工处置结论；同时把存量工单回填出对应事件。
