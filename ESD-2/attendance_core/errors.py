"""Domain errors, each carrying both its HTTP and its gRPC meaning.

The service raises these; each transport translates them using the two
attributes below. Keeping the mapping next to the error means the HTTP status
and the gRPC status can never silently drift apart.
"""


class DomainError(Exception):
    slug = "internal-error"          # last part of the problem 'type' URI
    title = "Internal error"
    http_status = 500
    grpc_code = "INTERNAL"           # name of a grpc.StatusCode member

    def __init__(self, detail, *, errors=None, headers=None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors          # optional list of {"field", "message"}
        self.headers = headers or {}


class ValidationError(DomainError):
    slug, title = "validation-error", "Invalid request"
    http_status, grpc_code = 400, "INVALID_ARGUMENT"


class NotFoundError(DomainError):
    slug, title = "not-found", "Resource not found"
    http_status, grpc_code = 404, "NOT_FOUND"


class AlreadyCheckedInError(DomainError):
    slug, title = "already-checked-in", "Already checked in"
    http_status, grpc_code = 409, "ALREADY_EXISTS"


class ScanNotMatchedError(DomainError):
    slug, title = "scan-not-matched", "Biometric scan not matched"
    http_status, grpc_code = 401, "UNAUTHENTICATED"


class EnrollmentUnavailableError(DomainError):
    slug, title = "enrollment-unavailable", "Enrollment temporarily unavailable"
    http_status, grpc_code = 503, "UNAVAILABLE"


class IdempotencyKeyReuseError(DomainError):
    slug, title = "idempotency-key-reuse", "Idempotency key reused with a different request"
    http_status, grpc_code = 422, "INVALID_ARGUMENT"


class RateLimitedError(DomainError):
    slug, title = "rate-limited", "Too many requests"
    http_status, grpc_code = 429, "RESOURCE_EXHAUSTED"
