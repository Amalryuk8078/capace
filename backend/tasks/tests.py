import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis
from django.db import connection
from freezegun import freeze_time

from tasks import services
from tasks.models import Task
from tasks.services import (
    HINTS,
    RULE_ALREADY_COMPLETED,
    RULE_DEADLINE,
    RULE_DEPENDENCY,
    RULE_ELIGIBILITY,
    RULE_LOCKED,
    CompletionRejected,
    TaskAlreadyCompleted,
    calculate_deadline,
    complete_task,
    create_task,
    is_eligible,
    register_creation,
)

LOW, MEDIUM, HIGH = Task.Priority.LOW, Task.Priority.MEDIUM, Task.Priority.HIGH
BASE = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
TASKS_URL = "/api/tasks/"


def complete_url(task_id):
    return f"/api/tasks/{task_id}/complete/"


def run_concurrently(count, target):
    """Run `target` in `count` threads at once and collect results or exceptions."""
    barrier = threading.Barrier(count)
    results, lock = [], threading.Lock()

    def worker():
        try:
            barrier.wait()
            result = target()
        except Exception as exc:
            result = exc
        finally:
            connection.close()
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


# --- Rule 3: deadline --------------------------------------------------------


class TestDeadline:
    def test_odd_minute_adds_duration_once(self):
        created = BASE.replace(minute=15)
        assert calculate_deadline(created, 10) == BASE.replace(minute=25)

    def test_even_minute_adds_duration_twice(self):
        created = BASE.replace(minute=16)
        assert calculate_deadline(created, 10) == BASE.replace(minute=36)

    def test_minute_is_read_in_configured_timezone(self, settings):
        # 10:00 UTC is 15:45 in Kathmandu (UTC+5:45): an odd local minute.
        settings.TIME_ZONE = "Asia/Kathmandu"
        assert calculate_deadline(BASE, 10) == BASE + timedelta(minutes=10)

    @pytest.mark.django_db
    def test_completion_one_microsecond_before_deadline_succeeds(self, make_task, odd_minute):
        task = make_task(created_at=odd_minute, estimated_time_minutes=10)
        now = task.deadline_at - timedelta(microseconds=1)
        assert complete_task(task.id, now=now).status == Task.Status.COMPLETED

    @pytest.mark.django_db
    def test_completion_exactly_at_deadline_is_rejected(self, make_task, odd_minute):
        task = make_task(created_at=odd_minute, estimated_time_minutes=10)
        with pytest.raises(CompletionRejected) as exc:
            complete_task(task.id, now=task.deadline_at)
        assert exc.value.rule == RULE_DEADLINE


# --- Rule 4: hidden eligibility ----------------------------------------------


class TestEligibility:
    def test_same_task_and_secret_always_give_same_result(self, settings):
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.5
        task_id = uuid.uuid4()
        assert len({is_eligible(task_id) for _ in range(20)}) == 1

    def test_ratio_controls_share_of_eligible_tasks(self, settings):
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.5
        share = sum(is_eligible(uuid.UUID(int=i)) for i in range(4000)) / 4000
        assert 0.45 < share < 0.55

    def test_result_depends_on_secret(self, settings):
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.5
        ids = [uuid.UUID(int=i) for i in range(200)]
        first = [is_eligible(i) for i in ids]
        settings.SMART_TASK_BOARD["ELIGIBILITY_SECRET"] = "another-secret"
        assert [is_eligible(i) for i in ids] != first

    @pytest.mark.django_db
    def test_ineligible_task_is_rejected(self, settings, make_task, odd_minute):
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.0
        task = make_task(created_at=odd_minute)
        with pytest.raises(CompletionRejected) as exc:
            complete_task(task.id, now=odd_minute + timedelta(minutes=1))
        assert exc.value.rule == RULE_ELIGIBILITY


# --- Rule 2: burst lock ------------------------------------------------------


class TestBurstLock:
    def test_fourth_creation_within_two_minutes_is_locked(self):
        results = [register_creation(BASE + timedelta(seconds=30 * i)) for i in range(4)]
        assert results == [False, False, False, True]

    def test_window_resets_after_a_lock(self):
        results = [register_creation(BASE + timedelta(seconds=i)) for i in range(8)]
        assert results == [False, False, False, True] * 2

    def test_creations_older_than_120_seconds_do_not_count(self):
        for seconds in (0, 10, 20):
            register_creation(BASE + timedelta(seconds=seconds))
        # The creation at 0s is exactly 120s old and has left the window.
        assert register_creation(BASE + timedelta(seconds=120)) is False

    def test_tracking_key_expires(self, fake_redis, settings):
        register_creation(BASE)
        ttl = fake_redis.pttl(settings.SMART_TASK_BOARD["BURST_REDIS_KEY"])
        assert 0 < ttl <= 120_000

    @pytest.mark.django_db
    def test_locked_task_gets_five_minute_lock_in_postgres(self, odd_minute):
        tasks = [
            create_task(title="t", priority=MEDIUM, estimated_time_minutes=30, now=odd_minute)
            for _ in range(4)
        ]
        stored = Task.objects.get(pk=tasks[3].pk)
        assert [t.lock_expires_at for t in tasks[:3]] == [None, None, None]
        assert stored.lock_expires_at == odd_minute + timedelta(minutes=5)

    @pytest.mark.django_db
    def test_completion_rejected_while_locked_and_allowed_after_expiry(self, make_task, odd_minute):
        task = make_task(
            created_at=odd_minute,
            estimated_time_minutes=60,
            lock_expires_at=odd_minute + timedelta(minutes=5),
        )
        with pytest.raises(CompletionRejected) as exc:
            complete_task(task.id, now=odd_minute + timedelta(minutes=4, seconds=59))
        assert exc.value.rule == RULE_LOCKED

        completed = complete_task(task.id, now=odd_minute + timedelta(minutes=5))
        assert completed.status == Task.Status.COMPLETED

    def test_concurrent_creations_lock_exactly_one_in_four(self):
        results = run_concurrently(12, lambda: register_creation(BASE))
        assert results.count(True) == 3

    @pytest.mark.django_db
    def test_redis_outage_fails_closed(self, monkeypatch, odd_minute):
        def broken():
            raise redis.ConnectionError("down")

        monkeypatch.setattr("tasks.services.get_redis", broken)
        with pytest.raises(services.BurstTrackerUnavailable):
            create_task(title="t", priority=LOW, estimated_time_minutes=5, now=odd_minute)
        assert Task.objects.count() == 0


# --- Rule 1: HIGH needs a completed LOW --------------------------------------


@pytest.mark.django_db
class TestHighPriorityDependency:
    def test_high_rejected_when_no_low_is_completed(self, make_task, odd_minute):
        make_task(priority=LOW, created_at=odd_minute)  # exists, but not completed
        high = make_task(priority=HIGH, created_at=odd_minute)
        with pytest.raises(CompletionRejected) as exc:
            complete_task(high.id, now=odd_minute + timedelta(minutes=1))
        assert exc.value.rule == RULE_DEPENDENCY

        high.refresh_from_db()
        assert high.status == Task.Status.PENDING
        assert high.completed_at is None
        assert high.failure_count == 1

    def test_high_allowed_after_a_low_is_completed(self, make_task, odd_minute):
        low = make_task(priority=LOW, created_at=odd_minute)
        high = make_task(priority=HIGH, created_at=odd_minute)
        complete_task(low.id, now=odd_minute + timedelta(minutes=1))
        assert complete_task(high.id, now=odd_minute + timedelta(minutes=2)).completed_at

    def test_completed_medium_does_not_count(self, make_task, odd_minute):
        make_task(priority=MEDIUM, created_at=odd_minute, completed=True)
        high = make_task(priority=HIGH, created_at=odd_minute)
        with pytest.raises(CompletionRejected):
            complete_task(high.id, now=odd_minute + timedelta(minutes=1))


# --- Completion service ------------------------------------------------------


@pytest.mark.django_db
class TestCompletion:
    def test_success(self, make_task, odd_minute):
        task = make_task(created_at=odd_minute)
        now = odd_minute + timedelta(minutes=1)
        completed = complete_task(task.id, now=now)
        assert completed.status == Task.Status.COMPLETED
        assert completed.completed_at == now

    def test_already_completed_is_rejected_without_counting_a_failure(self, make_task):
        task = make_task(completed=True)
        with pytest.raises(TaskAlreadyCompleted):
            complete_task(task.id)
        task.refresh_from_db()
        assert task.failure_count == 0

    def test_rejection_is_logged_internally_without_the_secret(
        self, settings, make_task, odd_minute, caplog
    ):
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.0
        task = make_task(created_at=odd_minute)
        with caplog.at_level("INFO"), pytest.raises(CompletionRejected):
            complete_task(task.id, now=odd_minute + timedelta(minutes=1))
        assert f"task_id={task.id} rule=hidden_eligibility" in caplog.text
        assert "test-secret" not in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rule", [RULE_LOCKED, RULE_DEADLINE, RULE_DEPENDENCY, RULE_ELIGIBILITY])
def test_rejected_task_is_never_completed_and_failure_is_committed(
    rule, settings, make_task, odd_minute
):
    """Real commits (transaction=True): the counter must survive, the status must not change."""
    task = make_task(
        priority=HIGH if rule == RULE_DEPENDENCY else MEDIUM,
        created_at=odd_minute,
        estimated_time_minutes=10,
        lock_expires_at=odd_minute + timedelta(minutes=5) if rule == RULE_LOCKED else None,
    )
    if rule == RULE_ELIGIBILITY:
        settings.SMART_TASK_BOARD["ELIGIBILITY_RATIO"] = 0.0
    now = task.deadline_at if rule == RULE_DEADLINE else odd_minute + timedelta(minutes=1)

    with pytest.raises(CompletionRejected) as exc:
        complete_task(task.id, now=now)

    assert exc.value.rule == rule
    stored = Task.objects.get(pk=task.pk)
    assert stored.status == Task.Status.PENDING
    assert stored.completed_at is None
    assert stored.failure_count == 1


@pytest.mark.redis_integration
def test_concurrent_creations_against_real_redis(monkeypatch, settings):
    """Run the Lua script on the real Redis from docker-compose (skipped if it is not running)."""
    client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis is not reachable at REDIS_URL")

    key = f"test:burst:{uuid.uuid4().hex}"
    settings.SMART_TASK_BOARD["BURST_REDIS_KEY"] = key
    monkeypatch.setattr("tasks.services.get_redis", lambda: client)
    try:
        # 18 creations with the *same* timestamp: members must still be distinct.
        results = run_concurrently(18, lambda: register_creation(BASE))
        assert results.count(True) == 4  # creations 4, 8, 12 and 16
        assert client.zcard(key) == 2  # creations 17 and 18 opened a new window
    finally:
        client.delete(key)


@pytest.mark.django_db(transaction=True)
class TestConcurrentCompletion:
    def test_only_one_of_many_concurrent_completions_succeeds(self, make_task, odd_minute):
        task = make_task(created_at=odd_minute)
        results = run_concurrently(
            6, lambda: complete_task(task.id, now=odd_minute + timedelta(minutes=1))
        )
        assert sum(isinstance(r, Task) for r in results) == 1
        assert sum(isinstance(r, TaskAlreadyCompleted) for r in results) == 5

    def test_concurrent_rejections_are_all_counted(self, make_task, odd_minute):
        high = make_task(priority=HIGH, created_at=odd_minute)
        results = run_concurrently(
            5, lambda: complete_task(high.id, now=odd_minute + timedelta(minutes=1))
        )
        assert all(isinstance(r, CompletionRejected) for r in results)
        high.refresh_from_db()
        assert high.failure_count == 5
        assert high.status == Task.Status.PENDING


# --- API -------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateAPI:
    def test_creates_task_with_server_generated_fields(self, api_client, odd_minute):
        payload = {
            "title": "Implement Django API",
            "priority": "HIGH",
            "estimated_time_minutes": 20,
            # Attempts to override server-generated fields are ignored.
            "created_at": "2000-01-01T00:00:00Z",
            "deadline_at": "2000-01-01T00:00:00Z",
            "status": "COMPLETED",
        }
        with freeze_time(odd_minute):
            response = api_client.post(TASKS_URL, payload, format="json")

        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "PENDING"
        assert data["created_at"].startswith(odd_minute.strftime("%Y-%m-%dT%H:%M"))
        assert data["time_remaining_seconds"] == 20 * 60
        assert data["is_locked"] is False
        assert Task.objects.get().deadline_at == odd_minute + timedelta(minutes=20)

    @pytest.mark.parametrize(
        "payload",
        [
            {"title": "", "priority": "LOW", "estimated_time_minutes": 5},
            {"title": "x" * 201, "priority": "LOW", "estimated_time_minutes": 5},
            {"title": "ok", "priority": "URGENT", "estimated_time_minutes": 5},
            {"title": "ok", "priority": "LOW", "estimated_time_minutes": 0},
            {"title": "ok", "priority": "LOW", "estimated_time_minutes": "soon"},
            {"priority": "LOW", "estimated_time_minutes": 5},
        ],
    )
    def test_invalid_data_returns_400(self, api_client, payload):
        response = api_client.post(TASKS_URL, payload, format="json")
        assert response.status_code == 400
        assert Task.objects.count() == 0

    def test_fourth_rapid_creation_is_locked(self, api_client):
        payload = {"title": "t", "priority": "LOW", "estimated_time_minutes": 30}
        responses = [api_client.post(TASKS_URL, payload, format="json") for _ in range(4)]
        assert [r.json()["is_locked"] for r in responses] == [False, False, False, True]
        assert responses[3].json()["lock_remaining_seconds"] in (299, 300)

    def test_redis_outage_returns_503(self, api_client, monkeypatch):
        def broken():
            raise redis.ConnectionError("connection refused on redis:6379")

        monkeypatch.setattr("tasks.services.get_redis", broken)
        payload = {"title": "t", "priority": "LOW", "estimated_time_minutes": 30}
        response = api_client.post(TASKS_URL, payload, format="json")
        assert response.status_code == 503
        assert "redis" not in response.content.decode().lower()
        assert Task.objects.count() == 0


@pytest.mark.django_db
class TestReadAPI:
    def test_list_and_retrieve(self, api_client, make_task):
        task = make_task()
        listing = api_client.get(TASKS_URL).json()
        assert listing["count"] == 1
        assert listing["results"][0]["id"] == str(task.id)
        assert api_client.get(f"{TASKS_URL}{task.id}/").json()["title"] == "Task"

    @pytest.mark.parametrize("task_id", ["not-a-uuid", str(uuid.uuid4())])
    def test_unknown_or_invalid_ids_return_404(self, api_client, task_id):
        assert api_client.get(f"{TASKS_URL}{task_id}/").status_code == 404
        assert api_client.post(complete_url(task_id)).status_code == 404

    def test_board_summary(self, api_client, make_task, odd_minute):
        make_task(priority=LOW, created_at=odd_minute, completed=True)
        make_task(created_at=odd_minute, lock_expires_at=odd_minute + timedelta(minutes=5))
        make_task(created_at=odd_minute, estimated_time_minutes=1)
        with freeze_time(odd_minute + timedelta(minutes=2)):
            data = api_client.get("/api/board/summary/").json()
        assert data == {"total": 3, "pending": 2, "completed": 1, "locked": 1, "expired": 1}

    def test_responses_never_mention_eligibility_or_secret(self, api_client, make_task):
        task = make_task()
        text = api_client.get(f"{TASKS_URL}{task.id}/").content.decode().lower()
        assert "eligib" not in text
        assert "secret" not in text


@pytest.mark.django_db
class TestCompleteAPI:
    def test_success(self, api_client, make_task):
        task = make_task()
        response = api_client.post(complete_url(task.id))
        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"

    def test_rule_failure_returns_cryptic_hint_with_409(self, api_client, make_task):
        high = make_task(priority=HIGH)
        response = api_client.post(complete_url(high.id))

        assert response.status_code == 409
        hint_code, message = HINTS[RULE_DEPENDENCY]
        assert response.json() == {"success": False, "message": message, "hint_code": hint_code}
        for leak in ("high", "low", "priority", "dependency"):
            assert leak not in response.content.decode().lower()

        high.refresh_from_db()
        assert high.status == Task.Status.PENDING

    def test_every_failure_maps_to_a_distinct_cryptic_hint(self):
        codes = {code for code, _ in HINTS.values()}
        assert len(codes) == len(HINTS) == 5
        forbidden = {"lock", "locked", "deadline", "priority", "eligible", "completed", "already"}
        for _, message in HINTS.values():
            assert not forbidden & set(message.lower().strip(".").split())

    def test_deadline_failure(self, api_client, make_task, odd_minute):
        task = make_task(created_at=odd_minute, estimated_time_minutes=1)
        with freeze_time(task.deadline_at):
            response = api_client.post(complete_url(task.id))
        assert response.json()["hint_code"] == HINTS[RULE_DEADLINE][0]

    def test_already_completed_also_returns_a_cryptic_hint(self, api_client, make_task):
        task = make_task(completed=True)
        response = api_client.post(complete_url(task.id))
        assert response.status_code == 409
        hint_code, message = HINTS[RULE_ALREADY_COMPLETED]
        assert response.json() == {"success": False, "message": message, "hint_code": hint_code}
        task.refresh_from_db()
        assert task.failure_count == 0

    def test_unexpected_error_returns_generic_500(self, api_client, make_task, monkeypatch):
        task = make_task()

        def explode(*args, **kwargs):
            raise RuntimeError("database password is hunter2")

        monkeypatch.setattr("tasks.services.complete_task", explode)
        response = api_client.post(complete_url(task.id))
        assert response.status_code == 500
        assert response.json() == {"success": False, "message": "An unexpected error occurred."}


# --- HTML board page ----------------------------------------------------------


def test_board_page_renders_with_csrf_token(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "<title>Smart Task Board</title>" in html
    assert 'name="csrf-token" content="' in html
    assert 'id="create-form"' in html
    # The page only talks to the API; no rule logic or secret is rendered.
    assert "/api/tasks/" in html
    assert "eligib" not in html.lower()
