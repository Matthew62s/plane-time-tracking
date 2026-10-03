/**
 * Copyright (c) 2026-present Hangar contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */
// @vitest-environment jsdom

import React, { act, useEffect, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { observer } from "mobx-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EIssueServiceType } from "@plane/types";
import usePeekOverviewOutsideClickDetector from "@/hooks/use-peek-overview-outside-click";
import { IssueDetail } from "@/store/issue/issue-details/root.store";
import type { IIssueRootStore } from "@/store/issue/root.store";
import { WorklogModal } from "./worklog-modal";

const state = vi.hoisted(() => ({
  detail: null as IssueDetail | null,
  createWorklog: vi.fn(),
  mutate: vi.fn(),
  fetchActivities: vi.fn(),
}));
vi.mock("@/lib/store-context", () => ({ StoreContext: null }));
vi.mock("@/hooks/store/use-issue-detail", () => ({ useIssueDetail: () => state.detail }));
vi.mock("@/hooks/store/use-member", () => ({ useMember: () => ({ getUserDetails: () => undefined }) }));
vi.mock("@/hooks/store/user", () => ({
  useUser: () => ({ data: { id: "user" } }),
  useUserPermissions: () => ({ getProjectRoleByWorkspaceSlugAndProjectId: () => 20 }),
}));
vi.mock("@plane/i18n", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("@plane/propel/toast", () => ({ TOAST_TYPE: { ERROR: "error" }, setToast: vi.fn() }));
vi.mock("@plane/propel/button", () => ({
  Button: ({
    children,
    loading: _loading,
    variant: _variant,
    size: _size,
    ...props
  }: React.PropsWithChildren<
    React.ButtonHTMLAttributes<HTMLButtonElement> & { loading?: boolean; variant?: string; size?: string }
  >) => <button {...props}>{children}</button>,
}));
vi.mock("@plane/ui", async () => ({
  ...(await vi.importActual<typeof import("@plane/ui")>("@plane/ui")),
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
  AlertModalCore: () => null,
}));
vi.mock("@/plane-web/hooks/use-worklogs", () => ({
  useWorklogs: () => ({ worklogs: [], totalDuration: 0, isLoading: false, mutate: state.mutate }),
}));
vi.mock("@/plane-web/services/worklog.service", () => ({
  worklogService: { createWorklog: state.createWorklog },
}));

let container: HTMLDivElement;
let root: Root;
let detail: IssueDetail;
const props = { workspaceSlug: "workspace", projectId: "project", issueId: "issue", canViewWorklogs: true };

// The peek's existing outside-click and Escape guards consume isAnyModalOpen.
// Keep its portal sibling outside the peek DOM, as on the task board.
const PeekHarness = observer(function PeekHarness() {
  const peekRef = useRef<HTMLDivElement>(null);
  const [peekOpen, setPeekOpen] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const isAnyModalOpen = detail.isAnyModalOpen;
  usePeekOverviewOutsideClickDetector(
    peekRef,
    () => {
      if (!isAnyModalOpen) setPeekOpen(false);
    },
    "issue"
  );
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isAnyModalOpen) setPeekOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [isAnyModalOpen]);
  return peekOpen ? (
    <div ref={peekRef} data-testid="peek">
      <button onClick={() => setModalOpen(true)}>Open worklogs</button>
      <WorklogModal {...props} isOpen={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  ) : null;
});

beforeEach(() => {
  vi.clearAllMocks();
  // Substores do not fetch until called; this suite exercises modal state only.
  detail = new IssueDetail({ rootStore: {} } as IIssueRootStore, EIssueServiceType.ISSUES);
  detail.fetchActivities = state.fetchActivities;
  state.detail = detail;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const render = async (element: React.ReactNode) => act(async () => root.render(element));
const click = async (element: Element) =>
  act(async () => {
    element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
const escape = async () =>
  act(async () => {
    document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
const open = async () => {
  await render(<PeekHarness />);
  await click(container.querySelector("button")!);
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
};

describe("worklog dialog in a task peek", () => {
  it("keeps the peek open when clicking either input and saves the duration", async () => {
    await open();
    const duration = document.querySelector<HTMLInputElement>('input[placeholder="2h 30m"]')!;
    const description = document.querySelector<HTMLInputElement>('input[placeholder="What was done? (optional)"]')!;
    await click(duration);
    await click(description);
    expect(container.querySelector('[data-testid="peek"]')).not.toBeNull();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(duration, "30m");
      duration.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click([...document.querySelectorAll("button")].find((button) => button.textContent === "Log time")!);
    expect(state.createWorklog).toHaveBeenCalledWith("workspace", "project", "issue", {
      duration: 30,
      description: "",
    });
    expect(container.querySelector('[data-testid="peek"]')).not.toBeNull();
  });

  it("closes the dialog on Escape and leaves the peek available for a second Escape", async () => {
    await open();
    await escape();
    expect(detail.isAnyModalOpen).toBe(false);
    // Allow Headless UI's leave transition to release the portal and focus trap.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[data-testid="peek"]')).not.toBeNull();
    await escape();
    expect(container.querySelector('[data-testid="peek"]')).toBeNull();
  });

  it("does not register closed or inaccessible dialogs and cleans up on unmount", async () => {
    await render(<WorklogModal {...props} isOpen={false} onClose={() => {}} />);
    expect(detail.isAnyModalOpen).toBe(false);
    await render(<WorklogModal {...props} isOpen onClose={() => {}} />);
    expect(detail.isAnyModalOpen).toBe(true);
    await render(<WorklogModal {...props} canViewWorklogs={false} isOpen onClose={() => {}} />);
    expect(detail.isAnyModalOpen).toBe(false);
    await render(<WorklogModal {...props} isOpen onClose={() => {}} />);
    await render(null);
    expect(detail.isAnyModalOpen).toBe(false);
  });

  it("keeps another open dialog registered when a sibling closes", async () => {
    const renderPair = (firstOpen: boolean, secondOpen: boolean) =>
      render(
        <React.StrictMode>
          <WorklogModal {...props} isOpen={firstOpen} onClose={() => {}} />
          <WorklogModal {...props} isOpen={secondOpen} onClose={() => {}} />
        </React.StrictMode>
      );
    await renderPair(true, true);
    expect(detail.openWorklogModalIds).toHaveLength(2);
    await renderPair(false, true);
    expect(detail.openWorklogModalIds).toHaveLength(1);
    expect(detail.isAnyModalOpen).toBe(true);
    await renderPair(false, false);
    expect(detail.isAnyModalOpen).toBe(false);
  });
});
