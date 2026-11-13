"""异常工单生命周期单测（SQLite 内存库，不依赖 MySQL）。

覆盖用户反馈的核心场景：
  1. 异常消失 → 自动关闭并留痕；再次出现 → 重新进入待处理且 occurrence +1，历史结论保留；
  2. 重复复扫结果不变 → 不新增工单、不新增事件、不覆盖既有结论；
  3. 重新解析重建章节行（id 全变）→ 同一逻辑异常按稳定 issue_key 归并，不重复建单；
  4. rescan_all 正确统计本次「新建 / 重新打开 / 自动关闭 / 未变化」数量；
  5. 人工处置写 resolved 事件，重复处置报 409；
  6. 删除章节只解除外键关联，工单与事件流保留。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.constants.enums import IssueEventType, IssueStatus
from src.models import Base
from src.models.chapter import Chapter
from src.models.chapter_issue import ChapterIssue
from src.models.episode import Episode
from src.models.feed import Feed
from src.models.issue_event import IssueEvent
from src.services import issue_service


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield session
    session.close()
    engine.dispose()


def _make_feed(db: Session) -> Feed:
    feed = Feed(
        code="FD-1",
        name="测试节目",
        show_name="测试节目",
        kind="rss",
        url="https://example.test/rss",
        fixture_path="fixtures/x.xml",
        cadence="weekly",
        host="主播",
    )
    db.add(feed)
    db.flush()
    return feed


def _make_episode(db: Session, feed: Feed, duration: int = 1800) -> Episode:
    episode = Episode(
        code="EP-1",
        feed_id=feed.id,
        guid="guid-1",
        title="测试单集",
        duration_seconds=duration,
        published_on=date(2026, 11, 1),
        show_notes="",
    )
    db.add(episode)
    db.flush()
    return episode


def _add_chapters(db: Session, episode: Episode, points: list[int | None]) -> list[Chapter]:
    chapters = []
    for ordinal, point in enumerate(points):
        chapters.append(
            Chapter(
                episode_id=episode.id,
                ordinal=ordinal,
                start_seconds=point,
                title=f"章节{ordinal + 1}",
                raw_text=f"raw-{ordinal}",
                parse_rule="mm_ss",
                confidence=0.9,
                source="parse" if point is not None else "unparsed",
            )
        )
    db.add_all(chapters)
    db.flush()
    return chapters


def _issues(db: Session, episode: Episode) -> list[ChapterIssue]:
    return list(db.scalars(select(ChapterIssue).where(ChapterIssue.episode_id == episode.id)))


def _events(db: Session, issue: ChapterIssue) -> list[IssueEvent]:
    return list(
        db.scalars(
            select(IssueEvent).where(IssueEvent.issue_id == issue.id).order_by(IssueEvent.id)
        )
    )


def _reset_chapters(db: Session, episode: Episode, points: list[int | None]) -> list[Chapter]:
    """模拟重新解析：删掉重建全部章节行（id 必然变化），先解除工单关联。"""
    old = list(db.scalars(select(Chapter).where(Chapter.episode_id == episode.id)))
    issue_service.detach_chapter_issues(db, [row.id for row in old])
    for row in old:
        db.delete(row)
    db.flush()
    return _add_chapters(db, episode, points)


# ---------------------------------------------------------------- 首次发现


def test_first_detect_creates_open_ticket_and_event(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])  # 120 < 300 → 一条乱序

    open_count = issue_service.sync_episode_issues(db, episode)

    assert open_count == 1
    tickets = _issues(db, episode)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.kind == "out_of_order"
    assert ticket.status == IssueStatus.OPEN.value
    assert ticket.occurrence_count == 1
    assert ticket.issue_key == "EP-1:out_of_order:ord2"
    events = _events(db, ticket)
    assert [event.event_type for event in events] == [IssueEventType.DETECTED.value]
    assert episode.issue_count == 1


# ---------------------------------------------------------------- 自动关闭


def test_issue_disappearing_is_auto_closed_with_event(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]

    # 修正乱序后重扫
    for index, point in enumerate([60, 120, 300, 500]):
        db.scalars(select(Chapter).where(Chapter.episode_id == episode.id)).all()[index].start_seconds = point
    db.flush()
    open_count = issue_service.sync_episode_issues(db, episode)

    assert open_count == 0
    db.refresh(ticket)
    assert ticket.status == IssueStatus.RESOLVED.value
    assert ticket.resolved_by == "system:rescan"
    assert ticket.resolution_note == "重新校验未再出现"
    assert [event.event_type for event in _events(db, ticket)] == [
        IssueEventType.DETECTED.value,
        IssueEventType.AUTO_CLOSED.value,
    ]
    assert episode.issue_count == 0


# ---------------------------------------------------------------- 重新打开


def test_resolved_issue_reappearing_reopens_and_keeps_history(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]

    # 人工处置：写下处置依据
    issue_service.resolve_issue(db, ticket.id, "已与剪辑确认保留原顺序", "analyst@podcastdig.io")
    db.flush()

    # 异常消失（自动关闭场景先不经过，直接让它再次出现）→ 重新解析后乱序回归
    _reset_chapters(db, episode, [60, 300, 120, 500])
    open_count = issue_service.sync_episode_issues(db, episode)

    assert open_count == 1
    tickets = _issues(db, episode)
    assert len(tickets) == 1  # 没有重复建单
    db.refresh(ticket)
    assert ticket.status == IssueStatus.OPEN.value
    assert ticket.occurrence_count == 2
    # 工单上一次处置字段已让位给新的待处理状态，但结论完整保留在事件流里
    assert ticket.resolved_by == ""
    assert ticket.resolution_note == ""
    event_types = [event.event_type for event in _events(db, ticket)]
    assert event_types == [
        IssueEventType.DETECTED.value,
        IssueEventType.RESOLVED.value,
        IssueEventType.REOPENED.value,
    ]
    resolved_event = _events(db, ticket)[1]
    assert resolved_event.note == "已与剪辑确认保留原顺序"
    assert resolved_event.actor == "analyst@podcastdig.io"
    reopened_event = _events(db, ticket)[2]
    assert reopened_event.detail["occurrence"] == 2
    assert reopened_event.detail["previous_resolution_note"] == "已与剪辑确认保留原顺序"


def test_auto_closed_ticket_reopens_on_recurrence(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)

    # 异常消失 → 自动关闭
    _reset_chapters(db, episode, [60, 120, 300, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]
    assert ticket.status == IssueStatus.RESOLVED.value

    # 平台改写简介后乱序再次出现
    _reset_chapters(db, episode, [60, 300, 120, 500])
    open_count = issue_service.sync_episode_issues(db, episode)

    assert open_count == 1
    db.refresh(ticket)
    assert ticket.status == IssueStatus.OPEN.value
    assert ticket.occurrence_count == 2
    assert [event.event_type for event in _events(db, ticket)] == [
        IssueEventType.DETECTED.value,
        IssueEventType.AUTO_CLOSED.value,
        IssueEventType.REOPENED.value,
    ]


# ---------------------------------------------------------------- 重复复扫零写入


def test_repeated_rescan_without_change_writes_nothing(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]
    first_seen = ticket.last_seen_at
    first_updated = ticket.updated_at
    issue_service.sync_episode_issues(db, episode)
    issue_service.sync_episode_issues(db, episode)

    tickets = _issues(db, episode)
    assert len(tickets) == 1
    assert db.scalar(select(func.count()).select_from(IssueEvent)) == 1  # 只有首次 detected
    assert tickets[0].status == IssueStatus.OPEN.value
    assert tickets[0].occurrence_count == 1
    # 快照无变化时行级零写入：时间戳不被刷新
    assert tickets[0].last_seen_at == first_seen
    assert tickets[0].updated_at == first_updated

    # 关闭后再复扫同样零写入
    _reset_chapters(db, episode, [60, 120, 300, 500])
    issue_service.sync_episode_issues(db, episode)
    issue_service.sync_episode_issues(db, episode)
    db.refresh(tickets[0])
    assert tickets[0].status == IssueStatus.RESOLVED.value
    assert db.scalar(select(func.count()).select_from(IssueEvent)) == 2  # detected + auto_closed


# ---------------------------------------------------------------- 重解析按稳定 key 归并


def test_reparse_rebuilds_rows_but_keeps_single_ticket(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    chapters = _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    first_key = _issues(db, episode)[0].issue_key
    first_chapter_id = chapters[2].id

    # 重新解析：章节行全部重建（外键先解除）；同一位置仍是乱序
    new_chapters = _reset_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)

    tickets = _issues(db, episode)
    assert len(tickets) == 1
    assert tickets[0].issue_key == first_key
    # 工单重新关联到了重建后的章节行（而不是悬空）
    assert tickets[0].chapter_id == new_chapters[2].id
    assert tickets[0].chapter_id is not None
    # 异常从未消失，状态与事件都不变
    assert tickets[0].status == IssueStatus.OPEN.value
    assert [event.event_type for event in _events(db, tickets[0])] == [IssueEventType.DETECTED.value]


# ---------------------------------------------------------------- 复扫统计口径


def test_rescan_all_counts_opened_reopened_closed_unchanged(db: Session):
    feed = _make_feed(db)
    episode_a = _make_episode(db, feed)
    episode_b = Episode(
        code="EP-2",
        feed_id=feed.id,
        guid="guid-2",
        title="第二集",
        duration_seconds=1800,
        published_on=date(2026, 11, 2),
        show_notes="",
    )
    db.add(episode_b)
    db.flush()
    # A：乱序；B：干净
    _add_chapters(db, episode_a, [60, 300, 120, 500])
    _add_chapters(db, episode_b, [60, 200, 400])
    first = issue_service.rescan_all(db)
    assert first["opened"] == 1
    assert first["reopened"] == 0
    assert first["auto_closed"] == 0
    assert first["unchanged"] == 0  # 首轮所有工单都是新建

    # A 修好（自动关闭 1），B 保持干净（无工单即不计数）
    _reset_chapters(db, episode_a, [60, 120, 300, 500])
    second = issue_service.rescan_all(db)
    assert second["auto_closed"] == 1
    assert second["opened"] == 0
    assert second["reopened"] == 0
    assert second["unchanged"] == 0

    # 乱序回归（重新打开 1）
    _reset_chapters(db, episode_a, [60, 300, 120, 500])
    third = issue_service.rescan_all(db)
    assert third["reopened"] == 1
    assert third["auto_closed"] == 0
    assert third["unchanged"] == 0

    # 再复扫一次：工单仍在 → 未变化 1
    fourth = issue_service.rescan_all(db)
    assert fourth["unchanged"] == 1
    assert fourth["opened"] == 0
    assert fourth["reopened"] == 0
    assert fourth["auto_closed"] == 0


# ---------------------------------------------------------------- 人工处置


def test_manual_resolve_writes_event_and_duplicate_conflicts(db: Session):
    from src.utils.errors import AppError

    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]

    result = issue_service.resolve_issue(db, ticket.id, "缺口已知会补章", "admin@podcastdig.io")
    assert result["status"] == IssueStatus.RESOLVED.value
    db.refresh(ticket)
    assert ticket.resolved_by == "admin@podcastdig.io"
    events = _events(db, ticket)
    assert events[-1].event_type == IssueEventType.RESOLVED.value
    assert events[-1].note == "缺口已知会补章"
    assert episode.issue_count == 0

    with pytest.raises(AppError) as exc:
        issue_service.resolve_issue(db, ticket.id, "再处置一次", "admin@podcastdig.io")
    assert exc.value.status_code == 409
    # 冲突时没有追加事件
    assert len(_events(db, ticket)) == 2


# ---------------------------------------------------------------- 删除章节保留工单


def test_deleting_chapter_detaches_ticket_but_keeps_history(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    chapters = _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]
    assert ticket.chapter_id == chapters[2].id

    detached = issue_service.detach_chapter_issues(db, [chapters[2].id])
    assert detached == 1
    db.refresh(ticket)
    assert ticket.chapter_id is None
    # 工单与事件流都还在
    assert len(_events(db, ticket)) == 1
    assert db.scalar(select(func.count()).select_from(ChapterIssue)) == 1


# ---------------------------------------------------------------- 复核经过查询


def test_issue_history_and_episode_history_return_full_timeline(db: Session):
    feed = _make_feed(db)
    episode = _make_episode(db, feed)
    _add_chapters(db, episode, [60, 300, 120, 500])
    issue_service.sync_episode_issues(db, episode)
    ticket = _issues(db, episode)[0]
    issue_service.resolve_issue(db, ticket.id, "人工结论", "analyst@podcastdig.io")

    history = issue_service.issue_history(db, ticket.id)
    assert history["issue"]["issue_key"] == ticket.issue_key
    assert [event["event_type"] for event in history["events"]] == [
        IssueEventType.DETECTED.value,
        IssueEventType.RESOLVED.value,
    ]
    assert history["events"][1]["actor"] == "analyst@podcastdig.io"
    assert history["events"][0]["event_type_label"] == "发现异常"

    episode_history = issue_service.episode_issue_history(db, episode)
    assert len(episode_history) == 1
    assert len(episode_history[0]["events"]) == 2
    assert episode_history[0]["occurrence_count"] == 1
