import StatusPill from './StatusPill';
import { ISSUE_EVENT_LABELS, ISSUE_EVENT_TONE } from '../../constants/enums';
import type { IssueEvent } from '../../types/models';

interface Props {
  events: IssueEvent[];
  compact?: boolean;
}

function actorText(event: IssueEvent): string {
  if (!event.actor) return '系统';
  if (event.actor === 'system:rescan' || event.actor === 'validator') {
    return event.actor === 'validator' ? '校验器' : '系统复扫';
  }
  return event.actor;
}

/** 异常复核经过：append-only 事件时间轴（发现 / 自动关闭 / 重新打开 / 人工处置）。 */
export default function IssueTimeline({ events, compact = false }: Props) {
  if (!events.length) {
    return <p className="muted issue-timeline-empty">暂无复核记录。</p>;
  }
  return (
    <ol className={`issue-timeline${compact ? ' issue-timeline-compact' : ''}`}>
      {events.map((event) => (
        <li key={event.id} className="issue-timeline-item">
          <div className="issue-timeline-dot" aria-hidden="true" />
          <div className="issue-timeline-body">
            <div className="issue-timeline-head">
              <StatusPill tone={ISSUE_EVENT_TONE[event.event_type] ?? 'muted'}>
                {event.event_type_label || ISSUE_EVENT_LABELS[event.event_type] || event.event_type}
              </StatusPill>
              <span className="mono muted issue-timeline-meta">
                {actorText(event)}
                {event.created_at ? ` · ${event.created_at}` : ''}
              </span>
            </div>
            {event.note && <p className="issue-timeline-note">{event.note}</p>}
            {event.event_type === 'reopened' && typeof event.detail?.occurrence === 'number' && (
              <p className="issue-timeline-sub muted mono">第 {Number(event.detail.occurrence)} 次出现</p>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
