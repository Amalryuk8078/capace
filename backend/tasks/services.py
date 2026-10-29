"""Business rules for the Smart Task Board.

Rule 1  HIGH-priority dependency    -> _violated_rule()
Rule 2  Fourth-task burst lock       -> register_creation()
Rule 3  Odd/even minute deadline     -> calculate_deadline()
Rule 4  Hidden completion eligibility -> is_eligible()
Rule 5  Cryptic failure hints        -> HINTS / CompletionRejected
"""

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timedelta
from functools import lru_cache

import redis
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from tasks.models import Task

logger = logging.getLogger(__name__)


# --- Exceptions -------------------------------------------------------------


class CompletionRejected(Exception):
    """Completion failed. Per rule 5 the client only ever sees the cryptic hint.

    ``rule`` is for server logs only.
    """

    def __init__(self, rule):
        super().__init__(rule)
        self.rule = rule
        self.hint_code, self.message = HINTS[rule]


class TaskAlreadyCompleted(CompletionRejected):
    """Completing a finished task also fails with a hint, but is not a failed attempt."""

    def __init__(self):
        super().__init__(RULE_ALREADY_COMPLETED)


class BurstTrackerUnavailable(Exception):
    """Redis is unreachable, so rule 2 cannot be enforced safely."""


# --- Rule 5: cryptic hints --------------------------------------------------

RULE_DEADLINE = "deadline_passed"
RULE_DEPENDENCY = "high_priority_needs_completed_low"
RULE_LOCKED = "burst_lock_active"
RULE_ELIGIBILITY = "hidden_eligibility"
RULE_ALREADY_COMPLETED = "already_completed"

HINTS = {
    RULE_DEADLINE: ("HINT_01", "The clock remembers what you missed."),
    RULE_DEPENDENCY: ("HINT_02", "A quiet beginning is still required."),
    RULE_LOCKED: ("HINT_03", "The path remains closed."),
    RULE_ELIGIBILITY: ("HINT_04", "Not every key fits every door."),
    RULE_ALREADY_COMPLETED: ("HINT_05", "What is done cannot be done again."),
}


# --- Rule 3: deadline -------------------------------------------------------


def calculate_deadline(created_at: datetime, estimated_time_minutes: int) -> datetime:
    """Odd minute: created + duration. Even minute: created + 2 x duration.

    The minute is read in the configured TIME_ZONE.
    """
    minute = timezone.localtime(created_at).minute
    multiplier = 1 if minute % 2 == 1 else 2
    return created_at + timedelta(minutes=estimated_time_minutes * multiplier)


# --- Rule 4: hidden eligibility ----------------------------------------------


def is_eligible(task_id: uuid.UUID) -> bool:
    """Deterministic: HMAC-SHA256(secret, task id) mapped to [0, 1) < ratio."""
    config = settings.SMART_TASK_BOARD
    digest = hmac.new(
        config["ELIGIBILITY_SECRET"].encode(), str(task_id).encode(), hashlib.sha256
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < config["ELIGIBILITY_RATIO"]


# --- Rule 2: burst lock (Redis) ----------------------------------------------

# Runs atomically inside Redis, so concurrent creations cannot both slip
# under the threshold. Returns 1 when the new task must be locked.
_REGISTER_CREATION_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
if redis.call('ZCARD', key) >= threshold then
    redis.call('DEL', key)  -- reset policy: the locked task starts a new window
    return 1
end
redis.call('ZADD', key, now_ms, ARGV[4])
redis.call('PEXPIRE', key, window_ms)
return 0
"""


@lru_cache(maxsize=1)
def get_redis():
    return redis.Redis.from_url(
        settings.REDIS_URL,
        socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_TIMEOUT_SECONDS,
    )


def register_creation(now: datetime) -> bool:
    """Record a creation and return True if this task must be locked."""
    config = settings.SMART_TASK_BOARD
    try:
        locked = get_redis().eval(
            _REGISTER_CREATION_LUA,
            1,
            config["BURST_REDIS_KEY"],
            int(now.timestamp() * 1000),
            config["BURST_WINDOW_SECONDS"] * 1000,
            config["BURST_THRESHOLD"],
            uuid.uuid4().hex,  # unique member, so equal timestamps are all counted
        )
    except redis.RedisError as exc:
        raise BurstTrackerUnavailable from exc
    return bool(locked)


# --- Creation ---------------------------------------------------------------


def create_task(*, title, priority, estimated_time_minutes, now=None) -> Task:
    """Create a task with a server-generated timestamp, deadline and lock.

    Redis is called before the database transaction. If it is down, the
    task is not created (rule 2 is mandatory, so we fail closed).
    """
    now = now or timezone.now()
    locked = register_creation(now)
    lock_seconds = settings.SMART_TASK_BOARD["BURST_LOCK_SECONDS"]
    return Task.objects.create(
        title=title,
        priority=priority,
        estimated_time_minutes=estimated_time_minutes,
        created_at=now,
        deadline_at=calculate_deadline(now, estimated_time_minutes),
        lock_expires_at=now + timedelta(seconds=lock_seconds) if locked else None,
    )


# --- Completion -------------------------------------------------------------


def _violated_rule(task: Task, now: datetime) -> str | None:
    if task.is_locked(now):
        return RULE_LOCKED
    if task.is_expired(now):
        return RULE_DEADLINE
    if task.priority == Task.Priority.HIGH:
        low_done = Task.objects.filter(
            priority=Task.Priority.LOW, status=Task.Status.COMPLETED
        ).exists()
        if not low_done:
            return RULE_DEPENDENCY
    if not is_eligible(task.id):
        return RULE_ELIGIBILITY
    return None


def complete_task(task_id, *, now=None) -> Task:
    """Attempt to complete a task.

    The row is locked with SELECT ... FOR UPDATE, so concurrent requests for
    the same task run one at a time and only one can succeed.

    Raises Task.DoesNotExist, TaskAlreadyCompleted or CompletionRejected.
    A rejection increments failure_count and leaves the task PENDING.
    """
    with transaction.atomic():
        task = Task.objects.select_for_update().get(pk=task_id)
        if task.status == Task.Status.COMPLETED:
            logger.info("Completion rejected task_id=%s rule=%s", task.id, RULE_ALREADY_COMPLETED)
            raise TaskAlreadyCompleted

        # Read the clock after the row lock, so time spent waiting counts.
        now = now or timezone.now()
        rule = _violated_rule(task, now)
        if rule is None:
            task.status = Task.Status.COMPLETED
            task.completed_at = now
            task.save(update_fields=["status", "completed_at"])
            return task

        Task.objects.filter(pk=task.pk).update(failure_count=F("failure_count") + 1)

    logger.info("Completion rejected task_id=%s rule=%s", task.id, rule)
    raise CompletionRejected(rule)


# --- Summary ----------------------------------------------------------------


def board_summary(now=None) -> dict:
    now = now or timezone.now()
    pending = Q(status=Task.Status.PENDING)
    return Task.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=pending),
        completed=Count("id", filter=Q(status=Task.Status.COMPLETED)),
        locked=Count("id", filter=pending & Q(lock_expires_at__gt=now)),
        expired=Count("id", filter=pending & Q(deadline_at__lte=now)),
    )
