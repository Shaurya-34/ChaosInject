class PortError(Exception):
    """Only fixed, sanitized public error codes cross the port boundary."""
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status


MESSAGES = {
    "NOT_FOUND": "Requested item is unavailable.",
    "MUTATIONS_DISABLED": "Mutations are disabled in this mode.",
    "EXPIRED": "Approval or proposal expired; validate again.",
    "CONFLICT": "State changed or another run is active.",
    "HASH_MISMATCH": "Exact proposal approval is required.",
    "APPROVAL_REQUIRED": "An unconsumed actor-bound approval is required.",
    "STOP_LATCHED": "STOP is latched; reconcile before resetting.",
    "SQL_UNAVAILABLE": "Bounded SQL execution is not integrated.",
    "REPLAY_READ_ONLY": "Replay data is read-only.",
    "RATE_LIMIT": "Too many requests; try again shortly.",
    "BODY_TOO_LARGE": "Request exceeds the 64 KiB limit.",
    "INVALID_REQUEST": "Request does not match the API contract.",
    "UNAUTHORIZED": "A valid local operator token is required.",
    "ORIGIN_REJECTED": "Browser origin is not allowed.",
    "DEPENDENCY_UNAVAILABLE": "A required module is unavailable; operation refused.",
    "INVALID_EVIDENCE": "Summary or analysis evidence failed validation.",
    "ACTIONS_DISABLED": "Adaptive actions are disabled for this workstream.",
}
