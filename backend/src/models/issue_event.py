"""章节异常复核事件（IssueEvent）：append-only 的完整复核经过。

每次「发现 / 自动关闭 / 重新打开 / 人工处置」都追加一行，永不更新、永不删除。
同一张工单（ChapterIssue，按 issue_key 稳定归并）可以经历多轮
发现 → 自动关闭 → 重新出现 → 再关闭/再处置，历史结论全部保留在这里。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class IssueEvent(Base):
    __tablename__ = "issue_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("chapter_issues.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
