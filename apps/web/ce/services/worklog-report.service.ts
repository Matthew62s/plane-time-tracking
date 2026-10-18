/**
 * Copyright (c) 2026-present Maciej Szymczak and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */
import { API_BASE_URL } from "@plane/constants";
import { APIService } from "@/services/api.service";

export type ReportFilters = { start: string; end: string; timezone: string; project: string; user: string };
export type Author = { logged_by_id: string | null; logged_by__display_name: string | null };
export type ReportRow = Author & {
  issue_id: string;
  issue__name: string;
  issue__sequence_id: number;
  project_id: string;
  project__name: string;
  project__identifier: string;
  duration: number;
  entries: number;
};
export type TimeReport = {
  total_duration: number;
  total_entries: number;
  timezone: string;
  users: (Author & { duration: number; entries: number })[];
  rows: ReportRow[];
  count: number;
  page: number;
  page_size: number;
  projects: { project_id: string; project__name: string }[];
  authors: Author[];
};
class WorklogReportService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
  async report(slug: string, filters: ReportFilters, page: number): Promise<TimeReport> {
    const params = Object.fromEntries(Object.entries({ ...filters, page }).filter(([, value]) => value !== ""));
    return (await this.get(`/api/workspaces/${slug}/worklog-report/`, { params })).data;
  }
  async export(slug: string, filters: ReportFilters): Promise<Blob> {
    const params = Object.fromEntries(
      Object.entries({ ...filters, export: "csv" }).filter(([, value]) => value !== "")
    );
    return (await this.get(`/api/workspaces/${slug}/worklog-report/`, { params, responseType: "blob" })).data;
  }
}
export const worklogReportService = new WorklogReportService();
