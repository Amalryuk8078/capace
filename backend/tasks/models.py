import uuid

from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Task(models.Model):
    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    priority = models.CharField(max_length=6, choices=Priority.choices)
    estimated_time_minutes = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    deadline_at = models.DateTimeField()
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"], name="task_created_at_idx"),
            # Rule 1 looks up completed LOW tasks.
            models.Index(fields=["priority", "status"], name="task_priority_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(estimated_time_minutes__gte=1),
                name="task_estimated_time_positive",
            ),
            models.CheckConstraint(
                condition=Q(deadline_at__gt=F("created_at")),
                name="task_deadline_after_creation",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="COMPLETED", completed_at__isnull=False)
                    | Q(status="PENDING", completed_at__isnull=True)
                ),
                name="task_completed_at_matches_status",
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.priority})"

    def is_locked(self, now):
        return self.lock_expires_at is not None and now < self.lock_expires_at

    def is_expired(self, now):
        return now >= self.deadline_at
