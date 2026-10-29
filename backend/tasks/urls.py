from django.urls import path
from rest_framework.routers import DefaultRouter

from tasks.views import BoardSummaryView, TaskViewSet

router = DefaultRouter()
router.register("tasks", TaskViewSet, basename="task")

urlpatterns = [
    path("board/summary/", BoardSummaryView.as_view(), name="board-summary"),
    *router.urls,
]
