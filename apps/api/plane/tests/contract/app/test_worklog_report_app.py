# Copyright (c) 2026-present Maciej Szymczak and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import csv
import io
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from plane.db.models import Issue, IssueAssignee, Project, ProjectMember, User, WorkspaceMember
from plane.ext.models import IssueWorkLog

pytestmark = [pytest.mark.django_db, pytest.mark.contract]


@pytest.fixture(autouse=True)
def no_tasks(mocker):
    mocker.patch("plane.db.mixins.soft_delete_related_objects.delay")


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(name="Report project", identifier="RPT", workspace=workspace)
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def issue(workspace, project):
    return Issue.objects.create(name="Report task", project=project, workspace=workspace)


def url(workspace):
    return f"/api/workspaces/{workspace.slug}/worklog-report/"


def log(issue, user, minutes=30, at=None):
    entry = IssueWorkLog.objects.create(
        issue=issue, project=issue.project, workspace=issue.workspace, logged_by=user, duration=minutes
    )
    if at:
        IssueWorkLog.objects.filter(pk=entry.pk).update(created_at=datetime.fromisoformat(at))
    return entry


def test_totals_author_not_assignees_and_csv(session_client, workspace, project, issue, create_user):
    other = User.objects.create(email=f"{uuid4()}@test.invalid", username=str(uuid4()), display_name="=FORMULA")
    IssueAssignee.objects.create(issue=issue, project=project, workspace=workspace, assignee=create_user)
    IssueAssignee.objects.create(issue=issue, project=project, workspace=workspace, assignee=other)
    log(issue, create_user, 90)
    log(issue, other, 30)
    log(issue, other, 10)
    response = session_client.get(url(workspace))
    assert response.status_code == 200
    data = response.data
    assert data["total_duration"] == 130
    assert data["total_entries"] == 3
    assert sum(r["duration"] for r in data["rows"]) == sum(u["duration"] for u in data["users"]) == 130
    assert len(data["rows"]) == 2
    exported = session_client.get(url(workspace), {"export": "csv"})
    rows = list(csv.DictReader(io.StringIO(b"".join(exported.streaming_content).decode("utf-8-sig"))))
    assert sum(int(r["Minutes"]) for r in rows) == 130
    assert any(r["User"] == "'=FORMULA" for r in rows)
    filtered = session_client.get(url(workspace), {"user": str(other.pk), "project": str(project.pk)}).data
    assert filtered["total_duration"] == 40
    assert len(filtered["rows"]) == 1


@pytest.mark.parametrize(
    "zone,start,end,inside,outside",
    [
        ("Europe/Moscow", "2026-10-01", "2026-10-01", "2026-10-01T20:59:59+00:00", "2026-10-01T21:00:00+00:00"),
        ("America/New_York", "2026-03-08", "2026-03-08", "2026-03-09T03:59:59+00:00", "2026-03-09T04:00:00+00:00"),
        ("America/New_York", "2026-11-01", "2026-11-01", "2026-11-02T04:59:59+00:00", "2026-11-02T05:00:00+00:00"),
    ],
)
def test_inclusive_last_day_dst(session_client, workspace, issue, create_user, zone, start, end, inside, outside):
    log(issue, create_user, 30, inside)
    log(issue, create_user, 60, outside)
    log(issue, create_user, 20, datetime.fromisoformat(inside).replace(day=1, month=1).isoformat())
    response = session_client.get(url(workspace), {"start": start, "end": end, "timezone": zone})
    assert response.status_code == 200
    assert response.data["total_duration"] == 30


def test_start_included_and_timezone_default(session_client, workspace, issue, create_user):
    create_user.user_timezone = "Europe/Moscow"
    create_user.save()
    log(issue, create_user, 45, "2026-09-30T21:00:00+00:00")
    log(issue, create_user, 60, "2026-09-30T20:59:59+00:00")
    result = session_client.get(url(workspace), {"start": "2026-10-01", "end": "2026-10-01"})
    assert result.data["total_duration"] == 45
    assert result.data["timezone"] == "Europe/Moscow"


@pytest.mark.parametrize(
    "params",
    [
        {"start": "garbage"},
        {"end": "9999-12-31"},
        {"start": "2026-10-02", "end": "2026-10-01"},
        {"timezone": "Mars/Base"},
        {"user": "bad"},
        {"project": "bad"},
        {"page": "0"},
        {"export": "xlsx"},
    ],
)
def test_validation(session_client, workspace, params):
    assert session_client.get(url(workspace), params).status_code == 400


def test_private_projects_and_inactive_memberships(session_client, workspace, project, issue, create_user):
    log(issue, create_user, 30)
    private = Project.objects.create(name="Secret", identifier="SEC", workspace=workspace, network=0)
    secret = Issue.objects.create(name="Secret task", project=private, workspace=workspace)
    log(secret, create_user, 900)
    response = session_client.get(url(workspace))
    assert response.data["total_duration"] == 30
    assert len(response.data["projects"]) == 1
    assert session_client.get(url(workspace), {"project": str(private.pk)}).data["total_duration"] == 0
    exported = session_client.get(url(workspace), {"export": "csv"})
    assert b"Secret" not in b"".join(exported.streaming_content)
    ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
    assert session_client.get(url(workspace)).data["total_duration"] == 0


@pytest.mark.parametrize("role,workspace_role,expected", [(5, 15, 0), (5, 20, 30), (15, 15, 30)])
def test_project_roles(session_client, workspace, project, issue, create_user, role, workspace_role, expected):
    log(issue, create_user)
    ProjectMember.objects.filter(project=project, member=create_user).update(role=role)
    WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=workspace_role)
    assert session_client.get(url(workspace)).data["total_duration"] == expected


@pytest.mark.parametrize("mode", ["guest", "inactive", "outsider", "anonymous"])
def test_workspace_access(session_client, workspace, project, issue, create_user, mode):
    log(issue, create_user)
    if mode == "guest":
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=5)
    elif mode == "inactive":
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
    elif mode == "outsider":
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).delete()
    else:
        session_client = APIClient()
    assert session_client.get(url(workspace)).status_code in (401, 403)


def test_historical_archives_disabled_deleted(session_client, workspace, project, issue, create_user):
    kept = log(issue, create_user, 45)
    project.is_time_tracking_enabled = False
    project.archived_at = datetime.now(timezone.utc)
    project.save()
    Issue.objects.filter(pk=issue.pk).update(archived_at="2026-10-01")
    assert session_client.get(url(workspace)).data["total_duration"] == 45
    IssueWorkLog.objects.filter(pk=kept.pk).update(deleted_at=datetime.now(timezone.utc))
    assert session_client.get(url(workspace)).data["total_duration"] == 0
    IssueWorkLog.all_objects.filter(pk=kept.pk).update(deleted_at=None)
    Issue.objects.filter(pk=issue.pk).update(deleted_at=datetime.now(timezone.utc))
    assert session_client.get(url(workspace)).data["total_duration"] == 0
    Issue.all_objects.filter(pk=issue.pk).update(deleted_at=None)
    Project.objects.filter(pk=project.pk).update(deleted_at=datetime.now(timezone.utc))
    assert session_client.get(url(workspace)).data["total_duration"] == 0


def test_deleted_author_preserved(session_client, workspace, issue, create_user):
    other = User.objects.create(email=f"{uuid4()}@test.invalid", username=str(uuid4()))
    entry = log(issue, other, 70)
    other.delete()
    entry.refresh_from_db()
    assert entry.logged_by_id is None
    result = session_client.get(url(workspace), {"user": "deleted"}).data
    assert result["total_duration"] == 70
    assert result["users"][0]["logged_by_id"] is None


def test_pagination_export_all_and_query_bound(
    session_client, workspace, project, issue, create_user, django_assert_max_num_queries
):
    issues = [Issue.objects.create(name=f"Task {i}", project=project, workspace=workspace) for i in range(105)]
    IssueWorkLog.objects.bulk_create(
        [IssueWorkLog(issue=i, project=project, workspace=workspace, logged_by=create_user, duration=5) for i in issues]
    )
    with django_assert_max_num_queries(12):
        first = session_client.get(url(workspace)).data
    second = session_client.get(url(workspace), {"page": 2}).data
    assert first["count"] == 105
    assert len(first["rows"]) == 100 and len(second["rows"]) == 5
    assert sum(r["duration"] for r in first["rows"] + second["rows"]) == first["total_duration"] == 525
    exported = session_client.get(url(workspace), {"export": "csv", "page": 2})
    rows = list(csv.DictReader(io.StringIO(b"".join(exported.streaming_content).decode("utf-8-sig"))))
    assert len(rows) == 105
    assert sum(int(r["Minutes"]) for r in rows) == 525


def test_other_workspace_filter_does_not_leak(session_client, workspace, issue, create_user):
    from plane.db.models import Workspace

    other = Workspace.objects.create(name="Other", slug="other-report", owner=create_user)
    project = Project.objects.create(name="Other project", identifier="OTH", workspace=other)
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    task = Issue.objects.create(name="Other task", project=project, workspace=other)
    log(task, create_user, 999)
    result = session_client.get(url(workspace), {"project": str(project.pk)}).data
    assert result["total_duration"] == 0
    assert result["rows"] == result["users"] == result["authors"] == result["projects"] == []


def test_empty_report(session_client, workspace):
    result = session_client.get(url(workspace)).data
    assert result["total_duration"] == result["total_entries"] == result["count"] == 0
    assert result["users"] == result["rows"] == []
