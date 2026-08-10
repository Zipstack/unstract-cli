"""The Phase 2 annotation for ``DeploymentExecution``, in its final form.

Applied at runtime by ``gen_docstudio_spec.py`` so the POC needs zero backend
changes. When this graduates, ``annotate()``'s body is pasted into
``api_v2/api_deployment_views.py`` verbatim (minus the function wrapper) and
this module is deleted.

``operation_id`` and ``tags`` decide the generated module path
(``api/<tag>/<operation_id>.py``) and therefore the CLI command tree.
"""


def annotate() -> None:
    from api_v2.api_deployment_views import DeploymentExecution
    from api_v2.serializers import ExecutionQuerySerializer, ExecutionRequestSerializer
    from drf_spectacular.types import OpenApiTypes
    from drf_spectacular.utils import (
        OpenApiParameter,
        extend_schema,
        extend_schema_field,
        extend_schema_view,
    )
    from rest_framework import serializers

    @extend_schema_field(OpenApiTypes.BINARY)
    class UploadField(serializers.FileField):
        """A bare ``FileField`` maps to ``format: uri`` — correct on output,
        wrong for a multipart upload, and generators emit ``str`` for it."""

    class ExecuteRequest(ExecutionRequestSerializer):
        """Subclasses the real serializer so every backend param arrives free."""

        # ``files`` arrives via ``request.FILES``, so no serializer declares it.
        files = serializers.ListField(child=UploadField(), required=False)

    class FileResult(serializers.Serializer):
        file = serializers.CharField()
        file_execution_id = serializers.CharField(required=False)
        status = serializers.CharField(required=False)
        result = serializers.JSONField(required=False)
        metadata = serializers.JSONField(required=False)
        metrics = serializers.JSONField(required=False)
        error = serializers.CharField(required=False, allow_null=True)

    class ExecutionMessage(serializers.Serializer):
        execution_status = serializers.CharField()
        execution_id = serializers.CharField(required=False)
        workflow_id = serializers.CharField(required=False)
        status_api = serializers.CharField(required=False, allow_null=True)
        error = serializers.CharField(required=False, allow_null=True)
        # The backend sends `result: null` while pending; without allow_null
        # the generated deserialiser iterates None and crashes.
        result = FileResult(many=True, required=False, allow_null=True)

    class ExecuteResponse(serializers.Serializer):
        message = ExecutionMessage()

    class StatusResponse(serializers.Serializer):
        status = serializers.CharField()
        message = FileResult(many=True, required=False, allow_null=True)

    class ErrorResponse(serializers.Serializer):
        status = serializers.CharField(required=False)
        message = serializers.JSONField(required=False, allow_null=True)

    path_params = [
        OpenApiParameter(
            "org_name",
            str,
            OpenApiParameter.PATH,
            description="Organization identifier.",
        ),
        OpenApiParameter(
            "api_name", str, OpenApiParameter.PATH, description="API deployment name."
        ),
    ]

    extend_schema_view(
        post=extend_schema(
            operation_id="execute",
            tags=["deployment"],
            parameters=path_params,
            request={"multipart/form-data": ExecuteRequest},
            responses={
                200: ExecuteResponse,
                422: ExecuteResponse,
                500: ErrorResponse,
            },
            description="Execute an API deployment against one or more files.",
        ),
        get=extend_schema(
            operation_id="status",
            tags=["deployment"],
            parameters=path_params + [ExecutionQuerySerializer],
            # 406 means the result was already consumed — this GET is one-shot.
            responses={
                200: StatusResponse,
                406: ErrorResponse,
                422: StatusResponse,
                500: ErrorResponse,
            },
            description="Poll the status of a previously started execution.",
        ),
    )(DeploymentExecution)
