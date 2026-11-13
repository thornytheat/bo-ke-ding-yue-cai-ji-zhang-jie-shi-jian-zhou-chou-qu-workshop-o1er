import { Fragment, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import ChapterTimeline from '../components/common/ChapterTimeline';
import EmptyState from '../components/common/EmptyState';
import IssueMixBar from '../components/common/IssueMixBar';
import IssueTimeline from '../components/common/IssueTimeline';
import PageHead from '../components/common/PageHead';
import StatusPill from '../components/common/StatusPill';
import TapeLabel from '../components/common/TapeLabel';
import { feedApi, summaryApi } from '../api/feeds';
import { episodeApi } from '../api/episodes';
import { issueApi } from '../api/issues';
import { exportApi } from '../api/exports';
import { useAsync } from '../hooks/useAsync';
import { useAuth } from '../router/guards';
import { can } from '../constants/roles';
import { ISSUES_HINT } from '../constants/ui';
import { ISSUE_KIND_LABELS, ISSUE_KIND_ORDER, ISSUE_SEVERITY_TONE } from '../constants/enums';
import { errorMessage } from '../utils/errorMessage';
import { uiStore } from '../stores/uiStore';
import type { EpisodeDetail, IssueItem, RescanResult } from '../types/models';

interface EpisodeGroup {
  episodeCode: string;
  episodeTitle: string;
  feedName: string;
  openCount: number;
  resolvedCount: number;
  issues: IssueItem[];
}

export default function Issues() {
  const state = useAuth();
  const canResolve = can(state.user?.role, 'issue:resolve');

  const [kind, setKind] = useState('');
  const [status, setStatus] = useState('open');
  const [feed, setFeed] = useState('');
  const [busy, setBusy] = useState(false);
  const [expandedCode, setExpandedCode] = useState<string | null>(null);
  const [detailCache, setDetailCache] = useState<Record<string, EpisodeDetail>>({});
  const [rescanResult, setRescanResult] = useState<RescanResult | null>(null);

  const toggleEpisode = async (code: string) => {
    if (expandedCode === code) {
      setExpandedCode(null);
      return;
    }
    setExpandedCode(code);
    if (!detailCache[code]) {
      try {
        const detail = await episodeApi.detail(code);
        setDetailCache((prev) => ({ ...prev, [code]: detail }));
      } catch (error) {
        uiStore.toast(errorMessage((error as { payload?: unknown })?.payload, '单集展开失败'), 'bad');
        setExpandedCode(null);
      }
    }
  };

  const summary = useAsync(() => issueApi.summary(), []);
  const feeds = useAsync(() => feedApi.list(), []);
  const overview = useAsync(() => summaryApi.overview(), []);
  const issues = useAsync(
    () => issueApi.list({ kind: kind || undefined, status: status || undefined, feed: feed || undefined, size: 200 }),
    [kind, status, feed],
  );

  const groups = useMemo<EpisodeGroup[]>(() => {
    const map = new Map<string, EpisodeGroup>();
    for (const issue of issues.data?.items ?? []) {
      let group = map.get(issue.episode_code);
      if (!group) {
        group = {
          episodeCode: issue.episode_code,
          episodeTitle: issue.episode_title,
          feedName: issue.feed_name,
          openCount: 0,
          resolvedCount: 0,
          issues: [],
        };
        map.set(issue.episode_code, group);
      }
      group.issues.push(issue);
      if (issue.status === 'open') group.openCount += 1;
      else group.resolvedCount += 1;
    }
    return [...map.values()].sort((a, b) => b.openCount - a.openCount || a.episodeCode.localeCompare(b.episodeCode));
  }, [issues.data]);

  const reloadAll = () => {
    issues.reload();
    summary.reload();
    overview.reload();
    // 展开中的单集详情也要刷新，事件时间轴才能看到刚刚的处置
    if (expandedCode) {
      episodeApi
        .detail(expandedCode)
        .then((detail) => setDetailCache((prev) => ({ ...prev, [expandedCode]: detail })))
        .catch(() => undefined);
    }
  };

  const resolve = async (id: number) => {
    setBusy(true);
    try {
      await issueApi.resolve(id, '异常队列人工确认');
      uiStore.toast('异常已处置');
      reloadAll();
    } catch (error) {
      uiStore.toast(errorMessage((error as { payload?: unknown })?.payload, '处置失败'), 'bad');
    } finally {
      setBusy(false);
    }
  };

  const rescan = async () => {
    setBusy(true);
    try {
      const result = await issueApi.rescan();
      setRescanResult(result);
      uiStore.toast(
        `全量复扫 ${result.episodes} 集：重新打开 ${result.reopened}，自动关闭 ${result.auto_closed}，未变化 ${result.unchanged}`,
      );
      reloadAll();
    } catch (error) {
      uiStore.toast(errorMessage((error as { payload?: unknown })?.payload, '复扫失败'), 'bad');
    } finally {
      setBusy(false);
    }
  };

  const download = async (kindName: 'clip' | 'chapter' | 'issuesCsv') => {
    setBusy(true);
    try {
      if (kindName === 'clip') {
        const size = await exportApi.clipList();
        uiStore.toast(`剪辑清单已下载（${Math.round(size / 1024)} KB）`);
      } else if (kindName === 'chapter') {
        const size = await exportApi.chapterCuts();
        uiStore.toast(`章节切点清单已下载（${Math.round(size / 1024)} KB）`);
      } else {
        const size = await exportApi.issues();
        uiStore.toast(`异常清单已下载（${Math.round(size / 1024)} KB）`);
      }
    } catch (error) {
      uiStore.toast(errorMessage((error as { payload?: unknown })?.payload, '导出失败'), 'bad');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead
        eyebrow="QC QUEUE · 异常队列"
        title="章节异常与剪辑清单"
        lede={ISSUES_HINT}
        meta={
          <>
            <span className="chip mono">{summary.data?.open ?? '—'} 待处理</span>
            <span className="chip mono">{summary.data?.gap ?? '—'} 缺口</span>
            <span className="chip mono">{summary.data?.unparsed ?? '—'} 未识别</span>
            <span className="chip mono">{overview.data?.chapters ?? '—'} 章</span>
          </>
        }
        actions={
          <>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => download('clip')}>
              导出剪辑清单 CSV
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => download('chapter')}>
              章节切点 CSV
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => download('issuesCsv')}>
              异常清单 CSV
            </button>
          </>
        }
      />

      <section className="issue-summary">
        <div className="issue-mix">
          <TapeLabel eyebrow="MIX · 异常构成" title="待处理分布" tone="plain" />
          <IssueMixBar data={summary.data?.by_kind ?? []} />
        </div>
        <div className="issue-note">
          <TapeLabel eyebrow="RULES · 判定口径" title="五条窗口不变量" tone="plain" />
          <ul className="rule-list">
            <li>
              <b>严格递增</b>：按 show notes 行序，时间点不得小于前文最大值 → 乱序
            </li>
            <li>
              <b>末章 ≤ 时长</b>：任何时间点超出单集时长 → 超时长
            </li>
            <li>
              <b>重叠合并</b>：同一时间点保留置信高者，标题用 / 合并
            </li>
            <li>
              <b>&lt; 20s 折叠</b>：窗口过短的章节折叠进前一章
            </li>
            <li>
              <b>缺口 &gt; 8min</b>：相邻章节空档过大 → 给出建议补章区间
            </li>
          </ul>
          {canResolve && (
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={rescan}>
              全量复扫
            </button>
          )}
        </div>
      </section>

      {rescanResult && (
        <section className="rescan-card">
          <div className="rescan-card-head">
            <TapeLabel
              eyebrow="RESCAN · 最近一次复扫结果"
              title={`扫描 ${rescanResult.episodes} 集`}
              tone="plain"
              meta={
                <button type="button" className="btn btn-quiet btn-sm" onClick={() => setRescanResult(null)}>
                  收起
                </button>
              }
            />
          </div>
          <div className="rescan-stats">
            <div className="rescan-stat rescan-stat-open">
              <span className="rescan-stat-num mono">{rescanResult.opened}</span>
              <span className="rescan-stat-label">本次新建</span>
            </div>
            <div className="rescan-stat rescan-stat-reopen">
              <span className="rescan-stat-num mono">{rescanResult.reopened}</span>
              <span className="rescan-stat-label">重新打开</span>
            </div>
            <div className="rescan-stat rescan-stat-closed">
              <span className="rescan-stat-num mono">{rescanResult.auto_closed}</span>
              <span className="rescan-stat-label">自动关闭</span>
            </div>
            <div className="rescan-stat rescan-stat-same">
              <span className="rescan-stat-num mono">{rescanResult.unchanged}</span>
              <span className="rescan-stat-label">未变化</span>
            </div>
          </div>
          <p className="rescan-foot muted">
            复扫后全站待处理 {rescanResult.open} 条、已处置 {rescanResult.resolved} 条；重复复扫在结果不变时不新增、不覆盖复核记录。
          </p>
        </section>
      )}

      <TapeLabel
        eyebrow="QUEUE · 工单"
        title="异常清单（按单集展开）"
        meta={
          <span className="mono">
            {issues.data?.total ?? 0} 条工单 / {groups.length} 个单集命中当前筛选
            {!canResolve && ' · 剪辑师只读'}
          </span>
        }
      />

      <div className="filters">
        <label className="field field-inline">
          <span>异常类型</span>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">全部类型</option>
            {ISSUE_KIND_ORDER.map((item) => (
              <option key={item} value={item}>
                {ISSUE_KIND_LABELS[item]}
              </option>
            ))}
          </select>
        </label>
        <label className="field field-inline">
          <span>处理状态</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部</option>
            <option value="open">待处理</option>
            <option value="resolved">已处置</option>
          </select>
        </label>
        <label className="field field-inline">
          <span>节目</span>
          <select value={feed} onChange={(event) => setFeed(event.target.value)}>
            <option value="">全部节目</option>
            {(feeds.data?.items ?? []).map((item) => (
              <option key={item.code} value={item.code}>
                {item.show_name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn-quiet" onClick={reloadAll}>
          刷新
        </button>
      </div>

      {issues.error ? (
        <EmptyState
          tone="error"
          title="异常队列读不出来"
          hint={errorMessage((issues.error as { payload?: unknown })?.payload, '接口返回异常')}
          actionLabel="重试"
          onAction={issues.reload}
        />
      ) : groups.length === 0 ? (
        <EmptyState
          title="这个筛选下没有异常"
          hint="试试把「处理状态」切到全部，或者去节目库挑一集看看时间轴。"
          actionLabel="看节目库"
          onAction={() => {
            window.location.href = '/library';
          }}
        />
      ) : (
        <div className="table-wrap">
          <table className="data-table issue-table">
            <thead>
              <tr>
                <th>类型</th>
                <th>单集</th>
                <th>位置</th>
                <th>说明</th>
                <th>建议</th>
                <th>状态 / 出现轮次</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => {
                const expanded = expandedCode === group.episodeCode;
                const detail = detailCache[group.episodeCode];
                return (
                  <Fragment key={group.episodeCode}>
                    <tr
                      className={`episode-group-row${expanded ? ' episode-group-row-open' : ''}`}
                      onClick={() => toggleEpisode(group.episodeCode)}
                    >
                      <td colSpan={4}>
                        <span className="episode-caret mono">{expanded ? '▾' : '▸'}</span>
                        <Link
                          className="table-link"
                          to={`/episodes/${group.episodeCode}`}
                          onClick={(event) => event.stopPropagation()}
                        >
                          <span className="mono">{group.episodeCode}</span>
                          <span className="table-sub">{group.episodeTitle}</span>
                        </Link>
                        <span className="table-sub muted">{group.feedName}</span>
                      </td>
                      <td colSpan={2}>
                        <span className="chip mono">{group.openCount} 待处理</span>
                        {group.resolvedCount > 0 && (
                          <span className="chip mono chip-soft">{group.resolvedCount} 已处置</span>
                        )}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn btn-quiet btn-sm"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleEpisode(group.episodeCode);
                          }}
                        >
                          {expanded ? '收起单集' : '展开单集'}
                        </button>
                      </td>
                    </tr>

                    {group.issues.map((issue) => (
                      <tr key={issue.id} className="issue-row">
                        <td>
                          <StatusPill tone={ISSUE_SEVERITY_TONE[issue.severity] ?? 'warn'}>{issue.kind_label}</StatusPill>
                        </td>
                        <td>
                          <span className="mono muted">{issue.episode_code}</span>
                        </td>
                        <td className="mono">{issue.chapter_time_tc ?? issue.window_start_tc ?? '—'}</td>
                        <td className="issue-cell">{issue.message}</td>
                        <td className="issue-cell muted">{issue.suggestion}</td>
                        <td>
                          {issue.status === 'open' ? (
                            <StatusPill tone="bad" dot>
                              待处理
                            </StatusPill>
                          ) : (
                            <StatusPill tone="ok">{issue.resolution_note || '已处置'}</StatusPill>
                          )}
                          {issue.occurrence_count > 1 && (
                            <span className="occur-badge mono" title="该异常消失后再次出现的累计轮次">
                              第 {issue.occurrence_count} 次出现
                            </span>
                          )}
                        </td>
                        <td>
                          {canResolve && issue.status === 'open' ? (
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              disabled={busy}
                              onClick={() => resolve(issue.id)}
                            >
                              处置
                            </button>
                          ) : (
                            <span className="muted mono">—</span>
                          )}
                        </td>
                      </tr>
                    ))}

                    {expanded && (
                      <tr className="episode-detail-row">
                        <td colSpan={7}>
                          {!detail ? (
                            <div className="skeleton-table">正在展开单集 …</div>
                          ) : (
                            <div className="episode-detail-pop">
                              <div className="episode-detail-section">
                                <div className="timeline-pop-head mono">
                                  {group.episodeCode} · {group.episodeTitle} · 时长 {detail.episode.duration_tc} ·
                                  当前 {detail.episode.issue_count} 条待处理
                                </div>
                                <ChapterTimeline
                                  compact
                                  duration={detail.episode.duration_seconds}
                                  chapters={detail.chapters}
                                />
                              </div>
                              <div className="episode-detail-section">
                                <div className="timeline-pop-head mono">REVIEW · 完整复核经过（按工单）</div>
                                {detail.issue_history.length === 0 ? (
                                  <p className="muted">本集没有异常工单，窗口不变量全部通过。</p>
                                ) : (
                                  <ul className="issue-review-list">
                                    {detail.issue_history.map((ticket) => (
                                      <li key={ticket.id} className="issue-review-card">
                                        <div className="issue-review-head">
                                          <StatusPill tone={ISSUE_SEVERITY_TONE[ticket.severity] ?? 'warn'}>
                                            {ticket.kind_label}
                                          </StatusPill>
                                          {ticket.status === 'open' ? (
                                            <StatusPill tone="bad" dot>
                                              待处理
                                            </StatusPill>
                                          ) : (
                                            <StatusPill tone="ok">已处置</StatusPill>
                                          )}
                                          <span className="chip mono chip-soft">累计出现 {ticket.occurrence_count} 次</span>
                                          {ticket.window_start_tc && (
                                            <span className="mono muted">{ticket.window_start_tc}</span>
                                          )}
                                        </div>
                                        <p className="issue-msg">{ticket.message}</p>
                                        <IssueTimeline events={ticket.events} compact />
                                      </li>
                                    ))}
                                  </ul>
                                )}
                              </div>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
