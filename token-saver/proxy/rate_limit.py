"""
Token-saver: rate-limited request queue with a circuit breaker.

This module implements:
- a bounded request queue that accepts / rejects new requests,
- a circuit breaker (opened after N failures within W seconds),
- and a /status endpoint that reports the circuit state.

The circuit breaker prevents a proxy thread from retrying requests
indefinitely when the upstream is simply not available for a minute or so.
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import get_settings

logger = logging.getLogger("token-saver.ratelimit")

# Circuit breaker thresholds.
CIRCUIT_OPEN_THRESHOLD = 5000  # requests that failed transiently in 60 seconds
CIRCUIT_HALF_OPEN_TIMEOUT = 30.0  # seconds before attempting partial reopen


class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN   = "open"         # reject all requests
    HALF_OPEN = "half_open"  # allow one request to test the upstream


@dataclass
class CircuitBreaker:
    """
    A simple circuit breaker that tracks transient failures and opens
    the circuit when its threshold is hit within a half-open timeout.
    """

    name: str = "compression-engine"
    failure_threshold: int = 5
    success_recovery_threshold: int = 1
    failure_timeout: float = 60.0  # reopen the circuit after 60s of success
    half_open_timeout: float = 30.0

    def __post_init__(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures: list[float] = []
        self._lock = threading.Lock()
        self._half_open_until: float = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            t = time.monotonic()
            # If we're in half-open mode and too long has passed, close it.
            if self._state == CircuitState.HALF_OPEN:
                if t >= self._half_open_until:
                    self._state = CircuitState.CLOSED
                    self._failures.clear()
            # In the closed state, trim old failures
            if self._state == CircuitState.CLOSED:
                self._failures[:] = [
                    f for f in self._failures
                    if t - f < self.failure_timeout
                ]
            self._failures.append(t)
            return self._state

    @property
    def info(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "failures_in_window": self.failure_threshold,
                "half_open_until": self._half_open_until,
            }

    def record_success(self) -> None:
        """Record a successful request; this closes the circuit for re-open testing."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                if self._failures:
                    self._failures.pop()  # drop the last recorded failure
                if len(self._failures) == 0:
                    self._state = CircuitState.CLOSED
                elif len(self._failures) > 0 and self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.CLOSED
                for f in self._failures:
                    self._failures.remove(f)

    async def run_with_circuit_breaker(
        self,
        fn: callable[..., Any],
        *args: Any,
    ) -> Any:
        """
        Run `fn(*args)` under the circuit breaker guard.

        If the circuit is OPEN and the half-open timeout has not passed,
        reject immediately. If the circuit is CLOSED/HALF_OPEN, call `fn`
        and record success/failure accordingly.
        """
        with self._lock:
            t = time.monotonic()
            # If OPEN, check if we can enter half-open.
            if self._state == CircuitState.OPEN:
                # Not yet in half-open; immediately reject.
                raise RuntimeError(
                    f"circuit breaker {self.name} is OPEN; reject request"
                )
            if self._state == CircuitState.HALF_OPEN:
                if t < self._half_open_until:
                    # Still in half-open; let it run.
                    pass
                else:
                    # Half-open timeout expired; mark as closed.
                    self._state = CircuitState.CLOSED
                    self._failures.clear()
                    self._half_open_until = 0.0
        # Run the function under the lock so that no other threads
        # can interleave between state check and state mutation.
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, fn, *args
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
                raise
            with self._lock:
                self._failures.append(t)
                self._half_open_until = t + self.half_open_timeout
                if len(self._failures) >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "opened circuit breaker; reject all upstream calls %s=%d failed",
                        f"circuit-breaker[{self.name}]",
                        self.failure_threshold,
                    )
            raise RuntimeError(
                f"circuit breaker {self.name} opened; {len(self._failures)} failures within {self.failure_timeout}s"
            )

    def close_half_open(self) -> None:
        """Close the circuit after a successful run — resets failures and clears half-open."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failures.clear()


# A simple bounded request queue that tracks pending requests and rejects
# new ones if the queue is full. Used to prevent a burst of requests from
# clobbering the request queue if the upstream is temporarily slow.

MAX_QUEUE_SIZE = 1024


class RequestQueue:
    """
    Bounded request queue that raises `queue.Full` when full.

    The caller must catch `queue.Empty` after the future it created is
    cancelled or timed out; a `queue` object is never reused.
    """

    def __init__(self, maxsize: int = MAX_QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._lock = asyncio.Lock()
        self._closed = False
        self._closed_event = asyncio.Event()

    async def put(self, obj: Any) -> None:
        """Enqueue `obj`. Raises `asyncio.QueueFull` if maxsize is hit."""
        while not self._closed:
            try:
                await self._queue.put(obj)
                return
            except asyncio.QueueFull:
                # Queue is full — reject immediately.
                raise asyncio.QueueFull(
                    f"request queue {id(self)} full"
                ) from None

    async def get(self, timeout: float | None = None) -> Any:
        try:
            obj = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            self._lock = asyncio.Lock()
            return obj
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("request timed out") from None

    async def put_nowait(self, obj: Any) -> None:
        """Blocking put that raises `asyncio.QueueFull` when full."""
        while not self._closed:
            try:
                await asyncio.wait_for(
                    self._queue.put(obj), timeout=0.1
                )
            except asyncio.QueueFull:
                # Keep spinning until a slot opens.
                continue
            except (asyncio.TimeoutError, asyncio.CancelledError):
                raise
            except RuntimeError:
                raise  # asyncio.QueueFull
        raise RuntimeError("request queue closed") from None

    def task_done(self) -> None:
        self._queue.task_done()

    # ---- Lifecycle ---- #

    def close(self) -> None:
        self._closed = True
        self._closed_event.set()
        logger.info("request queue closed")


# Global request queue instance.
_request_queue: RequestQueue | None = None


def create_request_queue(maxsize: int = MAX_QUEUE_SIZE) -> RequestQueue:
    global _request_queue
    with _rate_limit_lock:
        if _request_queue is None:
            _request_queue = RequestQueue(maxsize=maxsize)
        return _request_queue


def get_request_queue() -> RequestQueue:
    """Get the global request queue instance from config."""
    if _request_queue is None:
        _request_queue = create_request_queue(MAX_QUEUE_SIZE)
    return _request_queue


@dataclass
class RequestState:
    """Tracking struct for a single request's lifecycle within the queue."""

    started_at: float = field(default_factory=time.monotonic)
    started_by: str = field(default="", init=False)
    status: str = field(default="pending", init=False)
    error: str | None = field(default=None, init=False)


@dataclass
class RequestJob:
    """A single request queued for execution."""

    body: bytes
    req_model: str
    rate_limit_rate: float
    rate_limit_burst: int
    max_requests_per_second: float
    max_requests_per_minute: float
    circuit: CircuitBreaker
    queue: RequestQueue
    start: float = field(default_factory=time.monotonic)
    start_id: int = field(default_factory=lambda: id(_rate_limit_thread))
    status: str = field(default="pending")
    error: str | None = field(default=None)

    def __post_init__(self) -> None:
        now = time.monotonic()
        self._state = RequestState(
            started_at=now,
            started_by=f"job[{self.start_id}]",
            status="pending",
        )


# We keep a reference to the thread to keep the job alive while the thread runs.
_rate_limit_lock = threading.Lock()
_rate_limit_thread: threading.Thread | None = None
_rate_limit_stop = threading.Event()


def _stop_rate_limit_work() -> None:
    """Called from the main app loop when metrics are no longer needed."""
    global _rate_limit_thread
    if _rate_limit_thread is not None and _rate_limit_thread.is_alive():
        _rate_limit_thread.join(timeout=5.0)
    _rate_limit_thread = None
    _request_queue = None


# The rate limit worker thread polls the request queue and executes
# queued requests. It runs as a background thread and does not expose
# any public API to the main code base.


def _run_rate_limit_loop() -> None:
    """Background thread that drives rate limiting & circuit breaker enforcement."""
    global _rate_limit_thread

    rate_limit = config.get_settings()

    # Re-open the circuit breaker if it has been in the half-open state for a while.
    def _reopen_circuit_cb(_cb: threading.Timer = None) -> None:
        if _cb is None:
            raise RuntimeError("no circuit breaker")
        # When timeout fires, close the circuit again.
        cur_circuit = _cb.circuit
        if cur_circuit.state == CircuitState.HALF_OPEN:
            cur_circuit.close_half_open()
            return _reopen_circuit_cb(None)
        logger.info("reopened circuit breaker")
        _close_circuit_cb = threading.Timer(
            cur_circuit.failure_timeout,
            _reopen_circuit_cb,
        )
        _close_circuit_cb.circuit = cur_circuit
        _close_circuit_cb.start()

    while not _rate_limit_stop.is_set():
        try:
            # Try to grab a job from the queue.
            now = time.monotonic()
            job = _request_queue.get(timeout=0.25)
            if job is None:
                # No job available; loop back.
                continue
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            continue

        # Attempt to enqueue a new request and run it if it fits.
        now = time.monotonic()
        if now - job.start > rate_limit.rate_limit_window:
            # Job expired; clear.
            _request_queue.task_done()
            continue

        try:
            # Check if rate limit allows this request.
            if rate_limit.rate_limit_enabled:
                if rate_limit.rate_limit_count % rate_limit.rate_limit_window <= 0:
                    # This is the first request within the window.
                    pass
                elif rate_limit.rate_limit_count < rate_limit.max_requests_per_second:
                    pass

            # Advance the circuit breaker to allow execution.
            try:
                job._circuit.run_with_circuit_breaker(
                    job._fn,
                    *job._args,
                )
            except RuntimeError as exc:
                if "circuit breaker" in str(exc):
                    job._status = "rejected-circuit"
                    job._error = str(exc)
                elif "queue full" in str(exc).lower():
                    job._status = "rejected-queue"
                    job._error = "rate-limit queue full"
                else:
                    job._status = "rejected"
                    job._error = str(exc)

            job.start += rate_limit.rate_limit_window
        except Exception as exc:  # noqa: BLE001
            # The request failed; count it against the circuit breaker.
            job._circuit.record_failure()
            job._status = "rejected"
            job._error = str(exc)

        # Signal completion so the queue doesn't grow forever.
        _request_queue.task_done()

_thread: threading.Thread = None


def run_rate_limit_loop():
    """Start the rate limit worker thread."""
    global _rate_limit_thread
    _rate_limit_thread = threading.Thread(
        target=lambda: _run_rate_limit_loop(),
        name="rate-limiter",
        daemon=True,
    )
    _rate_limit_thread.start()


def stop_rate_limit_loop():
    """Stop the rate limit worker thread."""
    global _rate_limit_thread
    _rate_limit_stop.set()
    if _rate_limit_thread is not None:
        _rate_limit_thread.join(timeout=5)


_rate_limit_thread = None


_asyncio_event_loop = None
_event_loop_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


def get_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create the global asyncio event loop that runs inside the thread."""
    global _event_loop, _event_loop_lock, _rate_limit_thread
    with _event_loop_lock:
        if _event_loop is not None:
            return _event_loop
        if _rate_limit_thread is None:
            raise RuntimeError(
                "no event loop created — rate limiter not started"
            )
        _event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_event_loop)
        return _event_loop


async def _run_rate_limit_fn(job: RequestJob) -> None:
    """Executes a single request job and returns the response."""
    async with job._queue:
        job.result = await job._fn(*job._args)
        job._status = "ok"


def _make_async_fn(job: RequestJob) -> asyncio.Future:
    """Wrap the request job in an asyncio Future that signals when the job is done."""
    loop = get_event_loop()
    return loop.create_task(
        _run_rate_limit_fn(job),
        name=f"rate-limiter-job[{job.start_id}]",
    )


def submit_request(
    job: RequestJob,
    rate_limit: config.Settings,
) -> tuple[str, str]:
    """
    Enqueues a request into the queue and checks if the queue is full.

    If the queue is not full, the request is submitted and returns ("queued", "").
    If the queue is full, the request is dropped and returns ("full", "").

    Jobs are executed by a background thread that polls the request queue.
    The queue size must be configured to prevent runaway growth.
    """
    global _request_queue, _rate_limit_thread
    if _rate_limit_thread is None:
        raise RuntimeError("rate limiter not started")
    if _request_queue is None:
        raise RuntimeError("request queue not initialized")

    now = time.monotonic()
    if now - job.start > rate_limit.rate_limit_window:
        return ("rejected-expired", "request expired")

    _request_queue = RequestQueue(maxsize=rate_limit.max_queue_size)
    job.event = asyncio.get_event_loop().create_future()
    try:
        event_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running; create one.
        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)
    try:
        event_loop.create_task(
            _run_rate_limit_fn(job),
            name=f"rate-limiter-job[{job.start_id}]",
        )
    except RuntimeError:
        event_loop.close()
        _request_queue.task_done()
        return ("rejected-full", "queue full")

    return ("queued", "")


async def cancel_request(job: RequestJob) -> None:
    """Cancel a request job, if it is still pending or waiting in the queue."""
    global _request_queue
    if job.status == "completed":
        return
    if job.event is None or job.event.done():
        return

    try:
        job.event.cancel()
    except (asyncio.CancelledError, asyncio.CancelledError):
        pass

    async with _rate_limit_lock:
        if _request_queue is not None:
            await _request_queue.put_nowait(job)
            _request_queue.task_done()


# --------------------------------------------------------------------------- #
# API for the rest of the app
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Rate limit settings from the user-facing config."""

    rate_limit_enabled: bool = True
    max_requests_per_second: float = 10.0
    max_requests_per_minute: float = 500.0
    rate_limit_window: float = 60.0
    max_queue_size: int = 64

    # Circuit breaker limits
    circuit_open_threshold: int = 5000
    circuit_half_open_timeout: float = 30.0

    @property
    def rate_limit_time_window(self) -> float:
        return self.rate_limit_window

    @property
    def rate_limit_count(self) -> int:
        now = time.monotonic()
        window_start = now - self.rate_limit_window
        counter = 0
        for ts, _ in self._hits:
            if ts > window_start:
                counter += 1
        self._hits.append((now,))
        return counter


def setup_rate_limiting() -> None:
    """Run everything: create the request queue and start the background worker."""
    settings = config.get_settings()
    CircuitBreaker.name = "proxy.compression"


def get_rate_limit_settings() -> Settings:
    """Get rate limit settings from config."""
    settings = config.get_settings()
    return Settings(
        rate_limit_enabled=settings.rate_limit_enabled,
        max_requests_per_second=settings.max_requests_per_second,
        max_requests_per_minute=settings.max_requests_per_minute,
        rate_limit_window=settings.rate_limit_window,
        max_queue_size=settings.max_queue_size,
    )