"""异常工单服务：校验结果 ↔ chapter_issues 的同步、列表、处置、全量复扫。

核心口径（同一 issue_key 永远归并到同一张工单）：
  · 首次发现            → 建工单 + detected 事件，进待处理
  · 已关闭的异常再次出现 → 工单重新置 open + reopened 事件（occurrence_count + 1）
  · 异常消失（仍 open）  → 自动关闭 + auto_closed 事件，保留历史结论
  · 人工处置            → resolved 事件
  · 状态没变化的复扫    → 不写任何事件、不覆盖任何结论（重复复扫零写入）
每次「发现 / 自动关闭 / 重新打开 / 人工处置」的完整经过都在 issue_events 中追加留痕。
"""
from __future__ import annotations

from sqlalchemy import Select, func, select

from src.constants.enums import (
    ISSUE_EVENT_LABELS,
    ISSUE_KIND_LABELS,
    ISSUE_KIND_ORDER,
    IssueEventType,
    IssueKind,
    IssueStatus,
)
from src.models.chapter import Chapter
from src.models.chapter_issue import ChapterIssue
from src.models.episode import Episode
from src.models.feed import Feed
from src.models.issue_event import IssueEvent
from src.services import validation_service
from src.utils.errors import AppError
from src.utils.timecode import to_short_timecode
from src.utils.timeutil import local_iso, utcnow

SYSTEM_ACTOR = "system:rescan"
AUTO_CLOSE_NOTE = "重新校验未再出现"


def _add_event(
    db,
    issue: ChapterIssue,
    event_type: str,
    actor: str = "",
    note: str = "",
    detail: dict | None = None,
) -> IssueEvent:
    event = IssueEvent(
        issue_id=issue.id,
        event_type=event_type,
        actor=actor,
        note=(note or "")[:255],
        detail=detail or {},
    )
    db.add(event)
    return event


def _refresh_open_count(db, episode: Episode) -> int:
    episode.issue_count = (
        db.scalar(
            select(func.count())
            .select_from(ChapterIssue)
            .where(ChapterIssue.episode_id == episode.id, ChapterIssue.status == IssueStatus.OPEN.value)
        )
        or 0
    )
    return episode.issue_count


def sync_episode_issues(db, episode: Episode) -> int:
    """把一集的校验结果同步进工单表，返回当前待处理数。

    重复调用且校验结果不变时不产生任何写入（工单与事件都保持原样）。
    """
    chapters = list(
        db.scalars(select(Chapter).where(Chapter.episode_id == episode.id).order_by(Chapter.ordinal))
    )
    result = validation_service.validate_episode(
        episode.code,
        episode.duration_seconds,
        validation_service.chapter_inputs(chapters),
    )

    existing = {
        row.issue_key: row
        for row in db.scalars(select(ChapterIssue).where(ChapterIssue.episode_id == episode.id))
    }
    chapter_by_id = {chapter.id: chapter for chapter in chapters}
    now = utcnow()
    seen: set[str] = set()

    for draft in result.issues:
        seen.add(draft.issue_key)
        row = existing.get(draft.issue_key)
        if row is None:
            # 首次发现：建工单并记录发现事件
            row = ChapterIssue(
                issue_key=draft.issue_key,
                episode_id=episode.id,
                chapter_id=draft.chapter_id,
                kind=draft.kind,
                status=IssueStatus.OPEN.value,
                severity=draft.severity,
                message=draft.message,
                suggestion=draft.suggestion,
                window_start=draft.window_start,
                window_end=draft.window_end,
                detected_by="validator",
                occurrence_count=1,
                last_seen_at=now,
            )
            db.add(row)
            db.flush()
            _add_event(
                db,
                row,
                IssueEventType.DETECTED.value,
                actor="validator",
                note=draft.message,
                detail={"kind": draft.kind, "severity": draft.severity},
            )
            continue

        # 已存在的同逻辑异常：仅在快照确实变化时刷新（重复复扫做到行级零写入）
        snapshot_changed = (
            row.message != draft.message
            or row.suggestion != draft.suggestion
            or row.severity != draft.severity
            or row.window_start != draft.window_start
            or row.window_end != draft.window_end
        )
        if snapshot_changed:
            row.message = draft.message
            row.suggestion = draft.suggestion
            row.severity = draft.severity
            row.window_start = draft.window_start
            row.window_end = draft.window_end
            row.last_seen_at = now
        if draft.chapter_id is not None and draft.chapter_id in chapter_by_id and row.chapter_id != draft.chapter_id:
            row.chapter_id = draft.chapter_id

        if row.status == IssueStatus.RESOLVED.value:
            # 消失后再次出现：重新进入待处理，历史结论保留在事件流里
            row.status = IssueStatus.OPEN.value
            row.occurrence_count = (row.occurrence_count or 1) + 1
            row.last_seen_at = now
            previous_note = row.resolution_note
            row.resolved_by = ""
            row.resolved_at = None
            row.resolution_note = ""
            _add_event(
                db,
                row,
                IssueEventType.REOPENED.value,
                actor=SYSTEM_ACTOR,
                note="异常再次出现，重新进入待处理",
                detail={
                    "occurrence": row.occurrence_count,
                    "previous_resolution_note": previous_note,
                    "kind": draft.kind,
                },
            )

    for key, row in existing.items():
        if key in seen:
            continue
        if row.status != IssueStatus.OPEN.value:
            # 已关闭且本次仍未出现：状态未变化，零写入
            continue
        # 本次校验未再出现且仍待处理 → 自动关闭
        row.status = IssueStatus.RESOLVED.value
        row.resolved_by = SYSTEM_ACTOR
        row.resolved_at = now
        row.resolution_note = AUTO_CLOSE_NOTE
        _add_event(
            db,
            row,
            IssueEventType.AUTO_CLOSED.value,
            actor=SYSTEM_ACTOR,
            note=AUTO_CLOSE_NOTE,
            detail={"kind": row.kind},
        )

    db.flush()
    return _refresh_open_count(db, episode)


def rescan_delta(db, episode: Episode) -> dict[str, int]:
    """复扫单集并统计本次变化：新建 / 重新打开 / 自动关闭 / 未变化。"""
    before = {
        row.issue_key: row.status
        for row in db.scalars(select(ChapterIssue).where(ChapterIssue.episode_id == episode.id))
    }
    sync_episode_issues(db, episode)
    after_rows = list(
        db.scalars(
            select(ChapterIssue).where(ChapterIssue.episode_id == episode.id)
        )
    )
    delta = {"opened": 0, "reopened": 0, "auto_closed": 0, "unchanged": 0}
    for row in after_rows:
        old_status = before.get(row.issue_key)
        if old_status is None:
            delta["opened"] += 1
        elif old_status == IssueStatus.RESOLVED.value and row.status == IssueStatus.OPEN.value:
            delta["reopened"] += 1
        elif old_status == IssueStatus.OPEN.value and row.status == IssueStatus.RESOLVED.value:
            delta["auto_closed"] += 1
        else:
            delta["unchanged"] += 1
    return delta


def detach_chapter_issues(db, chapter_ids: list[int]) -> int:
    """章节行被删除/重建前，把引用它的工单 chapter_id 置空而不是删工单。

    工单按 issue_key 归并，删除章节行会让外键失效；置空后复核经过（事件流）
    完整保留，下一次校验会把仍存在的异常重新关联到新章节行。
    """
    ids = [chapter_id for chapter_id in chapter_ids if chapter_id is not None]
    if not ids:
        return 0
    rows = list(
        db.scalars(select(ChapterIssue).where(ChapterIssue.chapter_id.in_(ids)))
    )
    for row in rows:
        row.chapter_id = None
    db.flush()
    return len(rows)


def _issue_query(
    kind: str | None,
    status: str | None,
    episode_code: str | None,
    feed_code: str | None,
) -> Select:
    stmt = (
        select(ChapterIssue, Episode, Feed)
        .join(Episode, Episode.id == ChapterIssue.episode_id)
        .join(Feed, Feed.id == Episode.feed_id)
    )
    if kind:
        stmt = stmt.where(ChapterIssue.kind == kind)
    if status:
        stmt = stmt.where(ChapterIssue.status == status)
    if episode_code:
        stmt = stmt.where(Episode.code == episode_code)
    if feed_code:
        stmt = stmt.where(Feed.code == feed_code)
    return stmt


def list_issues(
    db,
    kind: str | None = None,
    status: str | None = None,
    episode_code: str | None = None,
    feed_code: str | None = None,
    page: int = 1,
    size: int = 50,
) -> dict:
    base = _issue_query(kind, status, episode_code, feed_code)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(ChapterIssue.status.asc(), ChapterIssue.kind.asc(), ChapterIssue.id.asc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    chapter_ids = [issue.chapter_id for issue, _, _ in rows if issue.chapter_id]
    chapters = {
        chapter.id: chapter
        for chapter in db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids)))
    } if chapter_ids else {}

    items = []
    for issue, episode, feed in rows:
        chapter = chapters.get(issue.chapter_id) if issue.chapter_id else None
        items.append(
            {
                "id": issue.id,
                "issue_key": issue.issue_key,
                "kind": issue.kind,
                "kind_label": ISSUE_KIND_LABELS.get(issue.kind, issue.kind),
                "status": issue.status,
                "severity": issue.severity,
                "message": issue.message,
                "suggestion": issue.suggestion,
                "window_start": issue.window_start,
                "window_end": issue.window_end,
                "window_start_tc": to_short_timecode(issue.window_start) if issue.window_start is not None else None,
                "episode_code": episode.code,
                "episode_id": episode.id,
                "episode_title": episode.title,
                "feed_code": feed.code,
                "feed_name": feed.name,
                "chapter_id": issue.chapter_id,
                "chapter_title": chapter.title if chapter else "",
                "chapter_time_tc": (
                    to_short_timecode(chapter.start_seconds)
                    if chapter and chapter.start_seconds is not None
                    else None
                ),
                "resolved_by": issue.resolved_by,
                "resolution_note": issue.resolution_note,
                "occurrence_count": issue.occurrence_count or 1,
                "created_at": local_iso(issue.created_at),
                "updated_at": local_iso(issue.updated_at),
                "last_seen_at": local_iso(issue.last_seen_at),
            }
        )

    return {"items": items, "total": total, "page": page, "size": size}


def _event_to_dict(event: IssueEvent) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "event_type_label": ISSUE_EVENT_LABELS.get(event.event_type, event.event_type),
        "actor": event.actor,
        "note": event.note,
        "detail": event.detail or {},
        "created_at": local_iso(event.created_at),
    }


def issue_history(db, issue_id: int) -> dict:
    issue = db.get(ChapterIssue, issue_id)
    if issue is None:
        raise AppError("NOT_FOUND", 404, detail=f"issue={issue_id}")
    episode = db.get(Episode, issue.episode_id)
    events = list(
        db.scalars(
            select(IssueEvent).where(IssueEvent.issue_id == issue.id).order_by(IssueEvent.id.asc())
        )
    )
    return {
        "issue": {
            "id": issue.id,
            "issue_key": issue.issue_key,
            "kind": issue.kind,
            "kind_label": ISSUE_KIND_LABELS.get(issue.kind, issue.kind),
            "status": issue.status,
            "severity": issue.severity,
            "message": issue.message,
            "suggestion": issue.suggestion,
            "occurrence_count": issue.occurrence_count or 1,
            "episode_code": episode.code if episode else None,
            "resolved_by": issue.resolved_by,
            "resolution_note": issue.resolution_note,
            "created_at": local_iso(issue.created_at),
            "last_seen_at": local_iso(issue.last_seen_at),
        },
        "events": [_event_to_dict(event) for event in events],
    }


def episode_issue_history(db, episode: Episode) -> list[dict]:
    """单集详情用：该集每张工单的当前状态 + 完整复核经过（按工单 id 排序）。"""
    issues = list(
        db.scalars(
            select(ChapterIssue)
            .where(ChapterIssue.episode_id == episode.id)
            .order_by(ChapterIssue.status.asc(), ChapterIssue.kind.asc(), ChapterIssue.id.asc())
        )
    )
    if not issues:
        return []
    events = list(
        db.scalars(
            select(IssueEvent)
            .where(IssueEvent.issue_id.in_([row.id for row in issues]))
            .order_by(IssueEvent.issue_id.asc(), IssueEvent.id.asc())
        )
    )
    events_by_issue: dict[int, list[IssueEvent]] = {}
    for event in events:
        events_by_issue.setdefault(event.issue_id, []).append(event)

    result = []
    for issue in issues:
        result.append(
            {
                "id": issue.id,
                "kind": issue.kind,
                "kind_label": ISSUE_KIND_LABELS.get(issue.kind, issue.kind),
                "status": issue.status,
                "severity": issue.severity,
                "message": issue.message,
                "suggestion": issue.suggestion,
                "window_start_tc": to_short_timecode(issue.window_start) if issue.window_start is not None else None,
                "resolution_note": issue.resolution_note,
                "resolved_by": issue.resolved_by,
                "occurrence_count": issue.occurrence_count or 1,
                "last_seen_at": local_iso(issue.last_seen_at),
                "events": [_event_to_dict(event) for event in events_by_issue.get(issue.id, [])],
            }
        )
    return result


def issue_summary(db) -> dict:
    kind_rows = db.execute(
        select(ChapterIssue.kind, ChapterIssue.status, func.count())
        .group_by(ChapterIssue.kind, ChapterIssue.status)
    ).all()
    counts: dict[str, dict[str, int]] = {kind: {"open": 0, "resolved": 0} for kind in ISSUE_KIND_ORDER}
    for kind, status, count in kind_rows:
        counts.setdefault(kind, {"open": 0, "resolved": 0})
        counts[kind][status] = counts[kind].get(status, 0) + count

    open_total = sum(item["open"] for item in counts.values())
    resolved_total = sum(item["resolved"] for item in counts.values())
    by_kind = [
        {
            "kind": kind,
            "label": ISSUE_KIND_LABELS.get(kind, kind),
            "open": counts.get(kind, {}).get("open", 0),
            "resolved": counts.get(kind, {}).get("resolved", 0),
            "total": counts.get(kind, {}).get("open", 0) + counts.get(kind, {}).get("resolved", 0),
        }
        for kind in ISSUE_KIND_ORDER
    ]
    return {
        "open": open_total,
        "resolved": resolved_total,
        "total": open_total + resolved_total,
        "by_kind": by_kind,
        "gap": counts.get(IssueKind.GAP_WARNING.value, {}).get("open", 0),
        "unparsed": counts.get(IssueKind.UNPARSED.value, {}).get("open", 0),
    }


def resolve_issue(db, issue_id: int, note: str, actor_email: str) -> dict:
    issue = db.get(ChapterIssue, issue_id)
    if issue is None:
        raise AppError("NOT_FOUND", 404, detail=f"issue={issue_id}")
    if issue.status == IssueStatus.RESOLVED.value:
        raise AppError("CONFLICT", 409, detail="该异常已处置")
    issue.status = IssueStatus.RESOLVED.value
    issue.resolved_by = actor_email
    issue.resolved_at = utcnow()
    issue.resolution_note = (note or "人工确认")[:255]
    db.flush()
    _add_event(
        db,
        issue,
        IssueEventType.RESOLVED.value,
        actor=actor_email,
        note=issue.resolution_note,
        detail={"kind": issue.kind, "occurrence": issue.occurrence_count or 1},
    )
    db.flush()

    episode = db.get(Episode, issue.episode_id)
    if episode is not None:
        _refresh_open_count(db, episode)
    return {"id": issue.id, "status": issue.status, "resolution_note": issue.resolution_note}


def rescan_all(db) -> dict:
    episodes = list(db.scalars(select(Episode).order_by(Episode.code)))
    totals = {"opened": 0, "reopened": 0, "auto_closed": 0, "unchanged": 0}
    for episode in episodes:
        delta = rescan_delta(db, episode)
        for key in totals:
            totals[key] += delta[key]
    return {"episodes": len(episodes), **totals, **issue_summary(db)}
