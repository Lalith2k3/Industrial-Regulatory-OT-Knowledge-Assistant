import math
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Sliding 60-second window over requests and tokens, with even spacing and an adaptive budget.

    Providers enforce limits more strictly than the nominal numbers suggest (observed on a Voyage
    3 RPM / 10K TPM account: about 5K tokens per minute got through). So requests are spaced at
    least 60/rpm seconds apart, and the working token budget adapts: each refusal lowers it, each
    accepted request raises it again, never above the nominal limit.
    """

    WINDOW_SECONDS = 60.0
    BACKOFF_FACTOR = 0.75  # budget multiplier after a refusal
    RECOVERY_FACTOR = 1.05  # budget multiplier after a success
    MIN_BUDGET_SHARE = 0.2  # never shrink below this share of the nominal limit

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if requests_per_minute <= 0 or tokens_per_minute <= 0:
            raise ValueError("rate limits must be positive")
        self.rpm = requests_per_minute
        self.tpm = tokens_per_minute
        self.budget = float(tokens_per_minute)
        self.min_interval = self.WINDOW_SECONDS / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._last_request = -math.inf

    def acquire(self, tokens: int) -> None:
        """Block until a request of `tokens` fits the window, the budget and the spacing."""
        while True:
            now = self._clock()
            while self._events and now - self._events[0][0] >= self.WINDOW_SECONDS:
                self._events.popleft()
            used = sum(t for _, t in self._events)
            fits = not self._events or (len(self._events) < self.rpm and used + tokens <= self.budget)
            spacing = self._last_request + self.min_interval - now
            if fits and spacing <= 0:
                self._last_request = now
                return
            wait = spacing if fits else max(spacing, self._events[0][0] + self.WINDOW_SECONDS - now)
            self._sleep(max(0.5, wait))

    def record(self, tokens: int) -> None:
        self._events.append((self._clock(), tokens))
        self.budget = min(float(self.tpm), self.budget * self.RECOVERY_FACTOR)

    def penalise(self, seconds: float = WINDOW_SECONDS) -> None:
        """After a refusal: block all requests for `seconds` and lower the working budget."""
        self._events.clear()
        self._events.append((self._clock() + seconds - self.WINDOW_SECONDS, self.tpm))
        self.budget = max(self.MIN_BUDGET_SHARE * self.tpm, self.budget * self.BACKOFF_FACTOR)
