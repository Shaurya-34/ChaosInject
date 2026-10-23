class PortError(Exception):
    """Only fixed, sanitized public error codes cross the API boundary."""
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status


MESSAGES = {
    "NOT_FOUND": "Requested item is unavailable.",
    "MUTATIONS_DISABLED": "Starting, stopping and recovering are disabled: the API was started without execution enabled.",
    "CONFLICT": "State changed or another run is active.",
    "BODY_TOO_LARGE": "Request exceeds the 64 KiB limit.",
    "INVALID_REQUEST": "Request does not match the API contract.",
    "UNAUTHORIZED": "A valid local operator token is required.",
    "ORIGIN_REJECTED": "Browser origin is not allowed.",
    "DEPENDENCY_UNAVAILABLE": "A required module is unavailable; operation refused.",
    "POLICY_REFUSED": "Experiment is not allowed by chaos/policy.json.",
}
