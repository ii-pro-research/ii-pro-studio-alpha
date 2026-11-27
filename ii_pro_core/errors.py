"""Public failures carry codes, never echo user payloads or executor stderr."""


class CoreError(Exception):
    pass


class ValidationError(CoreError):
    pass


class NotFound(CoreError):
    pass


class Conflict(CoreError):
    pass


class InvalidTransition(CoreError):
    pass


class ApprovalRequired(CoreError):
    pass


class UnsafePath(CoreError):
    pass


class ArtifactMismatch(CoreError):
    pass


class RecoveryRequired(CoreError):
    pass


class CoreBusy(CoreError):
    pass


class OwnershipLost(CoreBusy):
    """This Store's generation can no longer mutate Core state."""


class SchemaError(CoreError):
    pass
