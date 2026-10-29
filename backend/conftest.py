from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from rest_framework.test import APIClient

from tasks.models import Task
from tasks.services import calculate_deadline


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Replace the real Redis with an in-memory fake that also runs Lua scripts."""
    client = fakeredis.FakeRedis()
    monkeypatch.setattr("tasks.services.get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def rule_settings(settings):
    """Every task is eligible unless a test opts into the hidden rule."""
    settings.SMART_TASK_BOARD = {
        **settings.SMART_TASK_BOARD,
        "ELIGIBILITY_SECRET": "test-secret",
        "ELIGIBILITY_RATIO": 1.0,
    }
    return settings


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def odd_minute():
    """A future creation time at an odd minute (deadline = created + duration)."""
    return datetime.now(UTC).replace(minute=31, second=0, microsecond=0) + timedelta(hours=1)


@pytest.fixture
def make_task():
    def _make(
        *,
        priority=Task.Priority.MEDIUM,
        estimated_time_minutes=30,
        created_at=None,
        lock_expires_at=None,
        completed=False,
    ):
        created_at = created_at or datetime.now(UTC)
        return Task.objects.create(
            title="Task",
            priority=priority,
            estimated_time_minutes=estimated_time_minutes,
            created_at=created_at,
            deadline_at=calculate_deadline(created_at, estimated_time_minutes),
            lock_expires_at=lock_expires_at,
            status=Task.Status.COMPLETED if completed else Task.Status.PENDING,
            completed_at=created_at + timedelta(seconds=1) if completed else None,
        )

    return _make
