/**
 * Copyright (c) 2026-present Maciej Szymczak and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */
import { useState } from "react";
import useSWR from "swr";
import { useParams } from "react-router";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { Input } from "@plane/ui";
import { worklogReportService } from "@/plane-web/services/worklog-report.service";
import type { Author, ReportFilters } from "@/plane-web/services/worklog-report.service";
import { formatWorklogDuration } from "@/plane-web/helpers/worklog";

export function WorklogReport() {
  const { workspaceSlug = "" } = useParams();
  const { t } = useTranslation();
  const tr = (key: string) => t(`workspace_analytics.time_report.${key}`);
  const [filters, setFilters] = useState<ReportFilters>({
    start: "",
    end: "",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    project: "",
    user: "",
  });
  const [page, setPage] = useState(1);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(false);
  const { data, error, isLoading, mutate } = useSWR(
    ["worklog-report", workspaceSlug, filters, page],
    () => worklogReportService.report(workspaceSlug, filters, page),
    { revalidateOnFocus: true }
  );
  const update = (key: keyof ReportFilters, value: string) => {
    setFilters((old) => ({ ...old, [key]: value }));
    setPage(1);
    setExportError(false);
  };
  const author = (row: Author) => row.logged_by__display_name || tr(row.logged_by_id ? "unnamed_user" : "deleted_user");
  const download = async () => {
    setExporting(true);
    setExportError(false);
    try {
      const blob = await worklogReportService.export(workspaceSlug, filters);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "time-report.csv";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setExportError(true);
    } finally {
      setExporting(false);
    }
  };
  const selectClass = "rounded border border-subtle bg-surface-1 px-3 py-2 text-sm";
  return (
    <section className="space-y-6 p-6">
      <h2 className="text-xl font-semibold">{tr("title")}</h2>
      <p className="text-sm text-secondary">{tr("explanation")}</p>
      <div className="flex flex-wrap items-end gap-3">
        {(["start", "end"] as const).map((key) => (
          <label key={key} className="text-sm flex flex-col gap-1">
            {tr(key)}
            <Input type="date" value={filters[key]} onChange={(e) => update(key, e.target.value)} />
          </label>
        ))}
        <label className="text-sm flex flex-col gap-1">
          {tr("timezone")}
          <Input value={filters.timezone} onChange={(e) => update("timezone", e.target.value)} />
        </label>
        <label className="text-sm flex flex-col gap-1">
          {tr("project")}
          <select className={selectClass} value={filters.project} onChange={(e) => update("project", e.target.value)}>
            <option value="">{tr("all_projects")}</option>
            {data?.projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.project__name}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm flex flex-col gap-1">
          {tr("user")}
          <select className={selectClass} value={filters.user} onChange={(e) => update("user", e.target.value)}>
            <option value="">{tr("all_users")}</option>
            {data?.authors.map((a) => (
              <option key={a.logged_by_id ?? "deleted"} value={a.logged_by_id ?? "deleted"}>
                {author(a)}
              </option>
            ))}
          </select>
        </label>
        <Button variant="secondary" onClick={() => void mutate()}>
          {tr("refresh")}
        </Button>
        <Button variant="primary" disabled={!data || !!error || exporting} onClick={() => void download()}>
          {tr("export")}
        </Button>
      </div>
      {(error || exportError) && (
        <p role="alert" className="text-danger-primary">
          {tr("error")}
        </p>
      )}
      {isLoading && <p role="status">{tr("loading")}</p>}
      {data && !error && (
        <>
          <div className="rounded border border-subtle p-4">
            <span>{tr("total")}: </span>
            <strong>{formatWorklogDuration(data.total_duration)}</strong>
            <p className="text-sm text-secondary">
              {tr("entries")}: {data.total_entries}
            </p>
          </div>
          {data.total_entries === 0 ? (
            <p>{tr("empty")}</p>
          ) : (
            <>
              <h3 className="font-semibold">{tr("by_user")}</h3>
              <table className="text-sm w-full text-left">
                <thead>
                  <tr>
                    <th>{tr("user")}</th>
                    <th>{tr("duration")}</th>
                    <th>{tr("entries")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.users.map((row) => (
                    <tr key={row.logged_by_id ?? "deleted"} className="border-t border-subtle">
                      <td className="py-2">{author(row)}</td>
                      <td>{formatWorklogDuration(row.duration)}</td>
                      <td>{row.entries}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3 className="font-semibold">{tr("details")}</h3>
              <div className="overflow-x-auto">
                <table className="text-sm w-full text-left">
                  <thead>
                    <tr>
                      {["project", "issue", "user", "duration", "entries"].map((key) => (
                        <th key={key}>{tr(key)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row) => (
                      <tr key={`${row.issue_id}-${row.logged_by_id}`} className="border-t border-subtle">
                        <td className="py-2 pr-4">{row.project__name}</td>
                        <td className="pr-4">
                          {row.project__identifier}-{row.issue__sequence_id} {row.issue__name}
                        </td>
                        <td className="pr-4">{author(row)}</td>
                        <td>{formatWorklogDuration(row.duration)}</td>
                        <td>{row.entries}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center gap-3">
                <Button variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>
                  {tr("previous")}
                </Button>
                <span>
                  {page} / {Math.max(1, Math.ceil(data.count / data.page_size))}
                </span>
                <Button
                  variant="secondary"
                  disabled={page * data.page_size >= data.count}
                  onClick={() => setPage(page + 1)}
                >
                  {tr("next")}
                </Button>
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
