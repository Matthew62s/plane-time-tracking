/**
 * Copyright (c) 2026-present Maciej Szymczak and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */
// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorklogReport } from "./worklog-report";

const service = vi.hoisted(() => ({ report: vi.fn(), export: vi.fn() }));
vi.mock("@/plane-web/services/worklog-report.service", () => ({ worklogReportService: service }));
vi.mock("react-router", () => ({ useParams: () => ({ workspaceSlug: "team" }) }));
vi.mock("@plane/i18n", () => ({ useTranslation: () => ({ t: (key: string) => key.split(".").at(-1) }) }));
vi.mock("@plane/propel/button", () => ({
  Button: ({ variant: _, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string }) => (
    <button {...props} />
  ),
}));
vi.mock("@plane/ui", () => ({ Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} /> }));
let container: HTMLDivElement;
let root: Root;
const result = {
  total_duration: 90,
  total_entries: 2,
  timezone: "UTC",
  page: 1,
  page_size: 100,
  count: 101,
  projects: [{ project_id: "p", project__name: "Project" }],
  authors: [{ logged_by_id: null, logged_by__display_name: null }],
  users: [{ logged_by_id: null, logged_by__display_name: null, duration: 90, entries: 2 }],
  rows: [
    {
      issue_id: "i",
      issue__name: "Task",
      issue__sequence_id: 1,
      project_id: "p",
      project__name: "Project",
      project__identifier: "PR",
      logged_by_id: null,
      logged_by__display_name: null,
      duration: 90,
      entries: 2,
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  service.report.mockResolvedValue(result);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});
async function render() {
  await act(async () =>
    root.render(
      <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}>
        <WorklogReport />
      </SWRConfig>
    )
  );
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}
async function click(label: string) {
  const button = [...container.querySelectorAll("button")].find((b) => b.textContent === label)!;
  await act(async () => button.click());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}
it("shows historical deleted authors and paginates on the server", async () => {
  await render();
  expect(container.textContent).toContain("1h 30m");
  expect(container.textContent).toContain("deleted_user");
  expect(container.textContent).toContain("PR-1 Task");
  await click("next");
  expect(service.report.mock.calls.at(-1)?.[2]).toBe(2);
  const user = container.querySelectorAll("select")[1];
  await act(async () => {
    user.value = "deleted";
    user.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(service.report.mock.calls.at(-1)?.[1].user).toBe("deleted");
  expect(service.report.mock.calls.at(-1)?.[2]).toBe(1);
});
it("does not show totals or offer export after access fails", async () => {
  service.report.mockRejectedValue({ status: 403 });
  await render();
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("error");
  expect(container.textContent).not.toContain("1h 30m");
  expect([...container.querySelectorAll("button")].find((b) => b.textContent === "export")?.disabled).toBe(true);
});
it("shows empty results", async () => {
  service.report.mockResolvedValue({ ...result, total_duration: 0, total_entries: 0, rows: [], users: [], count: 0 });
  await render();
  expect(container.textContent).toContain("empty");
  expect(container.querySelector("table")).toBeNull();
});
it("exports the same filters and reports export failures", async () => {
  service.export.mockRejectedValue(new Error("unavailable"));
  await render();
  await click("export");
  expect(service.export).toHaveBeenCalledWith("team", service.report.mock.calls[0][1]);
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("error");
});
