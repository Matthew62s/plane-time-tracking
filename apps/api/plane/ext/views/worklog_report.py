# Copyright (c) 2026-present Maciej Szymczak and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Read-only historical time report. No assignee or membership joins on worklogs."""

import csv
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, Sum
from django.http import StreamingHttpResponse
from rest_framework import serializers
from rest_framework.response import Response

from plane.app.permissions import ROLE, allow_permission
from plane.app.views.base import BaseAPIView
from plane.db.models import ProjectMember, WorkspaceMember
from plane.ext.models import IssueWorkLog
from plane.utils.csv_utils import sanitize_csv_row


class ReportQuery(serializers.Serializer):
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    timezone = serializers.CharField(required=False, max_length=100)
    project = serializers.UUIDField(required=False)
    user = serializers.CharField(required=False, max_length=36)
    page = serializers.IntegerField(default=1, min_value=1, max_value=1000000)
    export = serializers.ChoiceField(choices=["csv"], required=False)

    def validate_user(self, value):
        if value == "deleted":
            return value
        return serializers.UUIDField().run_validation(value)

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Unknown IANA timezone.")
        return value

    def validate(self, attrs):
        if attrs.get("start") and attrs.get("end") and attrs["start"] > attrs["end"]:
            raise serializers.ValidationError("Start must not be after end.")
        if attrs.get("end") and attrs["end"].year == 9999:
            raise serializers.ValidationError("End date is out of range.")
        return attrs


DETAIL_FIELDS = (
    "issue_id",
    "issue__name",
    "issue__sequence_id",
    "project_id",
    "project__name",
    "project__identifier",
    "logged_by_id",
    "logged_by__display_name",
)


def detail_rows(queryset):
    return (
        queryset.order_by()
        .values(*DETAIL_FIELDS)
        .annotate(duration=Sum("duration"), entries=Count("id"))
        .order_by("project_id", "issue_id", "logged_by_id")
    )


class Echo:
    def write(self, value):
        return value


class WorklogReportEndpoint(BaseAPIView):
    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def get(self, request, slug):
        query = ReportQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        zone_name = params.get("timezone", request.user.user_timezone or "UTC")
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError({"timezone": "Unknown IANA timezone."})
        admin = WorkspaceMember.objects.filter(
            workspace__slug=slug, member=request.user, role=ROLE.ADMIN.value, is_active=True
        ).exists()
        memberships = ProjectMember.objects.filter(
            workspace__slug=slug,
            member=request.user,
            is_active=True,
            project__deleted_at__isnull=True,
        )
        if not admin:
            memberships = memberships.filter(role__in=[ROLE.ADMIN.value, ROLE.MEMBER.value])
        logs = IssueWorkLog.objects.filter(
            workspace__slug=slug,
            project_id__in=memberships.values("project_id"),
            project__workspace__slug=slug,
            issue__workspace__slug=slug,
            issue__deleted_at__isnull=True,
            project__deleted_at__isnull=True,
        )
        available = logs
        # Archives and disabled tracking retain historical time. Deleted parents do not.
        if params.get("start"):
            logs = logs.filter(created_at__gte=datetime.combine(params["start"], time.min, tzinfo=zone))
        if params.get("end"):
            logs = logs.filter(
                created_at__lt=datetime.combine(params["end"] + timedelta(days=1), time.min, tzinfo=zone)
            )
        if params.get("project"):
            logs = logs.filter(project_id=params["project"])
        if params.get("user"):
            logs = logs.filter(logged_by_id=None if params["user"] == "deleted" else params["user"])
        rows = detail_rows(logs)
        if params.get("export") == "csv":
            writer = csv.writer(Echo())

            def content():
                yield "\ufeff"
                yield writer.writerow(["Project", "Issue", "Issue name", "User", "User ID", "Minutes", "Entries"])
                for row in rows.iterator(chunk_size=1000):
                    yield writer.writerow(
                        sanitize_csv_row(
                            [
                                row["project__name"],
                                f"{row['project__identifier']}-{row['issue__sequence_id']}",
                                row["issue__name"],
                                row["logged_by__display_name"]
                                or ("Deleted user" if row["logged_by_id"] is None else "Unnamed user"),
                                str(row["logged_by_id"] or ""),
                                row["duration"],
                                row["entries"],
                            ]
                        )
                    )

            response = StreamingHttpResponse(content(), content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="time-report.csv"'
            return response
        projects = list(
            available.order_by("project__name", "project_id").values("project_id", "project__name").distinct()
        )
        users = list(
            available.order_by("logged_by__display_name", "logged_by_id")
            .values("logged_by_id", "logged_by__display_name")
            .distinct()
        )
        totals = logs.aggregate(duration=Sum("duration"), entries=Count("id"))
        summary = list(
            logs.order_by()
            .values("logged_by_id", "logged_by__display_name")
            .annotate(duration=Sum("duration"), entries=Count("id"))
            .order_by("-duration", "logged_by_id")
        )
        count = rows.count()
        offset = (params["page"] - 1) * 100
        return Response(
            {
                "total_duration": totals["duration"] or 0,
                "total_entries": totals["entries"],
                "users": summary,
                "rows": list(rows[offset : offset + 100]),
                "count": count,
                "page": params["page"],
                "page_size": 100,
                "timezone": zone_name,
                "date_field": "created_at",
                "projects": projects,
                "authors": users,
            }
        )
