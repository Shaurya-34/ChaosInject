"""Probe runner: drives the synthetic workload at a controlled rate.

Design notes worth knowing before you change anything here.

**Open-loop pacing with recorded deferral.** Arrivals are scheduled on a fixed
interval derived from the requested rate, not issued as fast as replies come
back. A purely closed-loop generator stops offering load exactly when the system
slows down, which hides the degradation we are trying to measure -- the classic
coordinated-omission trap. When concurrency is saturated we still count the
would-be arrival as ``deferred`` and surface it, rather than pretending the
offered rate was lower.

**Offered versus achieved rate.** Both are recorded. If they diverge, the
laptop, not the application, may be the bottleneck, and the phase summary says
so. Comparing a baseline at 10 rps against a fault window at 4 rps would be
meaningless, so the scorer checks profile equality.

**Session isolation.** Each concurrency slot owns a transport with its own
cookie jar, recycled every ``operations_per_session`` attempts. Synthetic carts
never mix, and no session identity outlives the run.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .clock import mono
from .config import WorkloadConfig
from .sampling import Sample
from .transport import HttpTransport
from .workload import OperationResult, WorkloadDefinition, execute

SampleSink = Callable[[Sample], None]


class TransportFactory(Protocol):
    def __call__(self) -> HttpTransport: ...


@dataclass
class ProbeStats:
    """What actually happened while driving load."""

    attempts: int = 0
    deferred: int = 0
    started_mono: float = 0.0
    ended_mono: float = 0.0
    warmup_attempts: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.ended_mono - self.started_mono)

    @property
    def achieved_rate_rps(self) -> float | None:
        if self.duration_seconds <= 0:
            return None
        return self.attempts / self.duration_seconds


class _Session:
    """One synthetic shopper. Owns a transport; recycled periodically."""

    def __init__(self, factory: TransportFactory, operations_per_session: int) -> None:
        self._factory = factory
        self._limit = operations_per_session
        self._used = 0
        self._transport: HttpTransport | None = None

    async def transport(self) -> HttpTransport:
        if self._transport is None or self._used >= self._limit:
            await self.close()
            self._transport = self._factory()
            self._used = 0
        self._used += 1
        return self._transport

    async def close(self) -> None:
        if self._transport is not None:
            try:
                await self._transport.aclose()
            finally:
                self._transport = None


class ProbeRunner:
    """Drives ``WorkloadDefinition`` and pushes ``Sample`` objects to a sink.

    The runner knows nothing about phases, faults or scoring. It produces
    samples; ``phases.PhaseRecorder`` decides which window they belong to. That
    separation is what lets a single continuous load stream be sliced against
    Tooshita's injection markers after the fact.
    """

    def __init__(
        self,
        definition: WorkloadDefinition,
        transport_factory: TransportFactory,
        sink: SampleSink,
        config: WorkloadConfig | None = None,
        *,
        operations_per_session: int = 12,
        seed: int | None = None,
    ) -> None:
        self._definition = definition
        self._factory = transport_factory
        self._sink = sink
        self._config = config or WorkloadConfig()
        self._operations_per_session = operations_per_session
        self._rng = random.Random(seed)
        self._stop = asyncio.Event()
        self._stats = ProbeStats()
        self._weights_cache: tuple[list[str], list[float]] | None = None

    # -- operation selection ---------------------------------------------

    def _pick_operation(self) -> str:
        if self._weights_cache is None:
            mix = self._config.operation_mix
            names = [n for n in mix if n in self._definition.operations]
            if not names:
                raise RuntimeError(
                    "operation_mix names none of the defined workload operations"
                )
            self._weights_cache = (names, [mix[n] for n in names])
        names, weights = self._weights_cache
        return self._rng.choices(names, weights=weights, k=1)[0]

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    @property
    def stats(self) -> ProbeStats:
        return self._stats

    async def run_for(self, seconds: float, *, warmup: bool = False) -> ProbeStats:
        """Drive load for ``seconds``.

        When ``warmup`` is true the samples are still emitted (so nothing is
        hidden) but the caller is expected not to open a measurement phase yet.
        Warm-up exists because first-request latency on a cold JIT, a cold cache
        and a freshly scheduled pod is not representative of steady state.
        """
        interval = 1.0 / self._config.requested_rate_rps
        semaphore = asyncio.Semaphore(self._config.concurrency)
        sessions = [
            _Session(self._factory, self._operations_per_session)
            for _ in range(self._config.concurrency)
        ]
        tasks: set[asyncio.Task] = set()

        self._stats.started_mono = mono()
        deadline = self._stats.started_mono + seconds
        next_arrival = self._stats.started_mono
        slot = 0

        try:
            while mono() < deadline and not self._stop.is_set():
                now = mono()
                if now < next_arrival:
                    await asyncio.sleep(min(next_arrival - now, deadline - now))
                    continue
                next_arrival += interval
                # Never let the schedule run away if we fell far behind; this is
                # recorded rather than silently absorbed.
                if next_arrival < mono():
                    next_arrival = mono()

                if semaphore.locked():
                    self._stats.deferred += 1

                session = sessions[slot % len(sessions)]
                slot += 1
                task = asyncio.create_task(
                    self._attempt(semaphore, session, warmup=warmup)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._stats.ended_mono = mono()
            for session in sessions:
                await session.close()

        if self._stats.deferred:
            self._stats.notes.append(
                f"{self._stats.deferred} scheduled arrivals were deferred because all "
                f"{self._config.concurrency} concurrency slots were busy; offered rate "
                "was not fully sustained"
            )
        return self._stats

    async def _attempt(
        self, semaphore: asyncio.Semaphore, session: _Session, *, warmup: bool
    ) -> None:
        async with semaphore:
            name = self._pick_operation()
            spec = self._definition.operations[name]
            context = self._definition.pick_context(self._rng)
            transport = await session.transport()
            result: OperationResult = await execute(spec, transport, context)

            self._stats.attempts += 1
            if warmup:
                self._stats.warmup_attempts += 1

            self._sink(
                Sample(
                    mono_t=mono(),
                    operation=result.operation,
                    outcome=result.outcome,
                    duration_ms=result.duration_ms,
                    error_category=result.error_category,
                )
            )
