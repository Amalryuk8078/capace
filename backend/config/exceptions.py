import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """DRF's default handling, plus a generic JSON 500 for unexpected errors.

    Internal details (stack traces, database or Redis errors) are logged on the
    server and never returned to the client.
    """
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return Response(
            {"success": False, "message": "An unexpected error occurred."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return response
