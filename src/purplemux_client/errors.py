class TerminalSessionError(RuntimeError):
    """Base error for PurpleMux terminal session operations."""


class SessionReadyTimeout(TerminalSessionError):
    """Raised when a PurpleMux session does not become ready in time."""


class WorkerFailure(TerminalSessionError):
    """Raised when a PurpleMux-backed worker or CLI operation fails."""


class WorkerNeedsInput(TerminalSessionError):
    """Raised when a worker cannot complete without additional input."""


class WorkerInterrupted(WorkerFailure):
    """Raised when a worker turn is interrupted."""


class ResultNotReady(WorkerFailure):
    """Raised when a fresh structured worker result is not ready."""


class MutationOutcomeUnknown(WorkerFailure):
    """Raised when a mutation may have been applied but cannot be confirmed."""
