import { http } from './client';
import type { IssueHistoryResponse, IssueItem, IssueSummary, Paginated, RescanResult } from '../types/models';

export interface IssueQuery {
  kind?: string;
  status?: string;
  episode?: string;
  feed?: string;
  page?: number;
  size?: number;
}

export const issueApi = {
  list: (query: IssueQuery = {}) => {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    });
    const suffix = params.toString() ? `?${params.toString()}` : '';
    return http.get<Paginated<IssueItem>>(`/issues${suffix}`);
  },
  summary: () => http.get<IssueSummary>('/issues/summary'),
  history: (id: number) => http.get<IssueHistoryResponse>(`/issues/${id}/history`),
  resolve: (id: number, note: string) =>
    http.post<{ issue: { id: number; status: string }; summary: IssueSummary }>(`/issues/${id}/resolve`, { note }),
  rescan: () => http.post<RescanResult>('/issues/rescan'),
};
