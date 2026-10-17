"""No-op mutation executor for the local prevention demo (dispatch must never run)."""


class MutationExecutor:
    """Spy-friendly stand-in for a real fault executor."""

    def __init__(self) -> None:
        self.dispatch_calls = 0

    @property
    def invocation_count(self) -> int:
        return self.dispatch_calls

    async def dispatch_service_pause(self, duration_seconds: int, target_id: str) -> None:
        self.dispatch_calls += 1
        raise RuntimeError("Target mutation dispatch is disabled in the local prevention demo")
