import uuid

from django.views.generic import TemplateView
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks import services
from tasks.models import Task
from tasks.serializers import (
    BoardSummarySerializer,
    CompletionFailureSerializer,
    TaskCreateSerializer,
    TaskSerializer,
)


class TaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """List, create, retrieve and complete tasks. Business rules live in services.py."""

    queryset = Task.objects.all()
    serializer_class = TaskSerializer

    def get_serializer_class(self):
        return TaskCreateSerializer if self.action == "create" else TaskSerializer

    @extend_schema(request=TaskCreateSerializer, responses={201: TaskSerializer})
    def create(self, request):
        payload = TaskCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            task = services.create_task(**payload.validated_data)
        except services.BurstTrackerUnavailable:
            return Response(
                {"success": False, "message": "Task creation is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(TaskSerializer(task).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=None,
        responses={200: TaskSerializer, 404: None, 409: CompletionFailureSerializer},
    )
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        try:
            task = services.complete_task(uuid.UUID(str(pk)))
        except (ValueError, Task.DoesNotExist):
            raise NotFound("Task not found.") from None
        except services.CompletionRejected as rejection:  # includes TaskAlreadyCompleted
            body = {
                "success": False,
                "message": rejection.message,
                "hint_code": rejection.hint_code,
            }
            return Response(body, status=status.HTTP_409_CONFLICT)
        return Response(TaskSerializer(task).data)


class BoardPageView(TemplateView):
    """HTML board. It only calls the API above; all rules stay on the server."""

    template_name = "tasks/board.html"


class BoardSummaryView(APIView):
    @extend_schema(responses=BoardSummarySerializer)
    def get(self, request):
        return Response(services.board_summary())
