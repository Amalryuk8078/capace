from django.utils import timezone
from rest_framework import serializers

from tasks.models import Task


class TaskCreateSerializer(serializers.Serializer):
    """Only these fields are accepted. Timestamps, deadline and lock are server-generated."""

    title = serializers.CharField(max_length=200)
    priority = serializers.ChoiceField(choices=Task.Priority.choices)
    estimated_time_minutes = serializers.IntegerField(min_value=1, max_value=24 * 60)


class TaskSerializer(serializers.ModelSerializer):
    is_locked = serializers.SerializerMethodField()
    lock_remaining_seconds = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    time_remaining_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "priority",
            "estimated_time_minutes",
            "created_at",
            "deadline_at",
            "status",
            "completed_at",
            "lock_expires_at",
            "is_locked",
            "lock_remaining_seconds",
            "is_expired",
            "time_remaining_seconds",
            "failure_count",
        ]
        read_only_fields = fields

    def _now(self):
        if "now" not in self.context:
            self.context["now"] = timezone.now()
        return self.context["now"]

    @staticmethod
    def _seconds_until(moment, now):
        return max(0, int((moment - now).total_seconds())) if moment else 0

    def _pending(self, task):
        return task.status == Task.Status.PENDING

    def get_is_locked(self, task) -> bool:
        return self._pending(task) and task.is_locked(self._now())

    def get_lock_remaining_seconds(self, task) -> int:
        if not self._pending(task):
            return 0
        return self._seconds_until(task.lock_expires_at, self._now())

    def get_is_expired(self, task) -> bool:
        return self._pending(task) and task.is_expired(self._now())

    def get_time_remaining_seconds(self, task) -> int:
        if not self._pending(task):
            return 0
        return self._seconds_until(task.deadline_at, self._now())


class CompletionFailureSerializer(serializers.Serializer):
    success = serializers.BooleanField(default=False)
    message = serializers.CharField()
    hint_code = serializers.CharField()


class BoardSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    pending = serializers.IntegerField()
    completed = serializers.IntegerField()
    locked = serializers.IntegerField()
    expired = serializers.IntegerField()
