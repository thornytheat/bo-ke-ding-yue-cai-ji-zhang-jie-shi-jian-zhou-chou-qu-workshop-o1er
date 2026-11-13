"""章节异常工单（ChapterIssue）：由不变量校验产出，可被人工处置。

issue_key 是对外稳定自然键（按单集 code + 异常类型 + 位置/时间锚点），
同一逻辑异常在重新解析、复扫前后都会归并到同一张工单，不随章节行 id 变化。

工单是「可反复打开」的：异常消失时自动关闭，再次出现时重新置为 open，
每一轮发现 / 自动关闭 / 重新打开 / 人工处置都在 issue_events 里留痕，
occurrence_count 记录这是该异常第几次出现。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ChapterIssue(Base):
    __tablename__ = "chapter_issues"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), index=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    severity: Mapped[str] = mapped_column(String(12), default="warn")
    message: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    window_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_by: Mapped[str] = mapped_column(String(24), default="validator")
    # 最近一次关闭的处置人 / 时间 / 依据（可能被下一轮重新打开清空，完整历史见 issue_events）
    resolved_by: Mapped[str] = mapped_column(String(160), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(255), default="")
    # 该异常第几次出现：首次发现 = 1，每次消失后再次出现 +1
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
