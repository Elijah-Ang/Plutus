from __future__ import annotations

import contextlib
import signal
from collections.abc import Iterator


class WallClockTimeout(BaseException):
    """Raised when a guarded runtime section exceeds its wall-clock budget."""


@contextlib.contextmanager
def wall_clock_timeout(seconds: float | int | None, label: str) -> Iterator[None]:
    seconds_float = float(seconds or 0)
    if seconds_float <= 0:
        yield
        return
    try:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        def _raise_timeout(signum, frame):  # noqa: ANN001
            raise WallClockTimeout(f"{label} exceeded {seconds_float:.0f}s")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds_float)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
