class DomainError(Exception):
    status_code = 400


class NotFoundError(DomainError):
    status_code = 404


class AuthenticationError(DomainError):
    status_code = 401


class AuthorizationError(DomainError):
    status_code = 403


class ConflictError(DomainError):
    status_code = 409


class InsufficientCreditsError(ConflictError):
    pass


class VerificationError(DomainError):
    status_code = 422
