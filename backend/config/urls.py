from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from tasks.views import BoardPageView

urlpatterns = [
    path("", BoardPageView.as_view(), name="board"),
    path("api/", include("tasks.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
