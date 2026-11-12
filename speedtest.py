from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, TextIO

from pydantic import (
    BaseModel, ConfigDict, HttpUrl, NonNegativeFloat, NonNegativeInt,
    PositiveFloat, PositiveInt, ValidationError, computed_field,
)

BITS_IN_MEGABIT = 1_000_000
BYTES_IN_MEGABYTE = 1_000_000


class FetchError(Exception):
    """Сетевой сбой при скачивании одного ресурса."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #

class TestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    runs: PositiveInt = 10
    timeout: PositiveFloat = 60.0
    connections: PositiveInt = 1


class RequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: PositiveInt
    bytes_downloaded: NonNegativeInt
    elapsed_s: NonNegativeFloat

    @computed_field
    @property
    def speed_mbps(self) -> float:
        if self.elapsed_s <= 0:
            return 0.0
        return self.bytes_downloaded * 8 / self.elapsed_s / BITS_IN_MEGABIT


class TestReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    results: tuple[RequestResult, ...]
    wall_clock_seconds: NonNegativeFloat       # реальное время всего прогона

    @computed_field
    @property
    def total_bytes(self) -> int:
        return sum(r.bytes_downloaded for r in self.results)

    @computed_field
    @property
    def total_request_seconds(self) -> float:
        # Сумма per-request времён. В параллели это НЕ wall-clock.
        return sum(r.elapsed_s for r in self.results)

    @computed_field
    @property
    def avg_request_seconds(self) -> float:
        return self.total_request_seconds / len(self.results) if self.results else 0.0

    @computed_field
    @property
    def aggregate_speed_mbps(self) -> float:
        # Настоящая пропускная способность: байты / wall-clock.
        if self.wall_clock_seconds <= 0:
            return 0.0
        return self.total_bytes * 8 / self.wall_clock_seconds / BITS_IN_MEGABIT

    @computed_field
    @property
    def aggregate_speed_mbs(self) -> float:
        if self.wall_clock_seconds <= 0:
            return 0.0
        return self.total_bytes / self.wall_clock_seconds / BYTES_IN_MEGABYTE

    @computed_field
    @property
    def min_speed_mbps(self) -> float:
        return min((r.speed_mbps for r in self.results), default=0.0)

    @computed_field
    @property
    def max_speed_mbps(self) -> float:
        return max((r.speed_mbps for r in self.results), default=0.0)


# --------------------------------------------------------------------------- #
# Fetcher strategy (без изменений)
# --------------------------------------------------------------------------- #

class Fetcher(ABC):
    @abstractmethod
    def fetch(self, url: str, timeout: float) -> tuple[int, float]: ...


class UrllibFetcher(Fetcher):
    USER_AGENT = "speedtest-cli/2.0"
    EXTRA_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}

    def fetch(self, url: str, timeout: float) -> tuple[int, float]:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.USER_AGENT, **self.EXTRA_HEADERS},
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"{url}: {exc}") from exc
        return len(payload), time.perf_counter() - start


# --------------------------------------------------------------------------- #
# Reporter strategy
# --------------------------------------------------------------------------- #

class Reporter(ABC):
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def on_start(self, config: TestConfig) -> None: ...           # noqa: B027
    def on_request(self, result: RequestResult) -> None: ...      # noqa: B027

    @abstractmethod
    def render(self, report: TestReport) -> None: ...


class ConsoleReporter(Reporter):
    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream)
        self._completed = 0
        self._total = 0

    def on_start(self, config: TestConfig) -> None:
        self._completed = 0
        self._total = config.runs
        mode = (f"sequential" if config.connections == 1
                else f"parallel, {config.connections} connections")
        print(f"URL:      {config.url}", file=self._stream)
        print(f"Requests: {config.runs}  ({mode})", file=self._stream)
        print(file=self._stream)

    def on_request(self, result: RequestResult) -> None:
        self._completed += 1
        print(
            f"  [{self._completed:>2}/{self._total}] "
            f"{result.elapsed_s:7.3f} s  "
            f"{format_bytes(result.bytes_downloaded):>10}  "
            f"~{result.speed_mbps:8.2f} Mbps",
            file=self._stream,
        )

    def render(self, report: TestReport) -> None:
        line = "─" * 62
        w = self._stream.write
        w("\n" + line + "\n")
        w(f"Total downloaded:      {format_bytes(report.total_bytes)}\n")
        w(f"Wall clock:            {report.wall_clock_seconds:.3f} s\n")
        w(f"Sum of request times:  {report.total_request_seconds:.3f} s\n")
        w(f"Avg request time:      {report.avg_request_seconds:.3f} s\n")
        w(f"Aggregate speed:       {report.aggregate_speed_mbps:.2f} Mbps  "
          f"({report.aggregate_speed_mbs:.2f} MB/s)\n")
        w(f"Per-request min/max:   {report.min_speed_mbps:.2f} / "
          f"{report.max_speed_mbps:.2f} Mbps\n")


class JsonReporter(Reporter):
    def render(self, report: TestReport) -> None:
        print(report.model_dump_json(indent=2), file=self._stream)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

class SpeedTest:
    def __init__(
        self,
        config: TestConfig,
        fetcher: Fetcher,
        reporter: Reporter | None = None,
        clock: Callable[[], float] = time.perf_counter,   # NEW — для тестов
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._reporter = reporter
        self._clock = clock

    def run(self) -> TestReport:
        if self._reporter is not None:
            self._reporter.on_start(self._config)

        wall_start = self._clock()
        if self._config.connections == 1:
            results = self._run_sequential()
        else:
            results = self._run_parallel()
        wall = self._clock() - wall_start

        report = TestReport(
            url=self._config.url,
            results=tuple(results),
            wall_clock_seconds=wall,
        )
        if self._reporter is not None:
            self._reporter.render(report)
        return report

    def _measure(self) -> tuple[int, float]:
        return self._fetcher.fetch(str(self._config.url), self._config.timeout)

    def _emit(self, index: int, size: int, elapsed: float) -> RequestResult:
        result = RequestResult(
            index=index, bytes_downloaded=size, elapsed_s=elapsed,
        )
        if self._reporter is not None:
            self._reporter.on_request(result)
        return result

    def _run_sequential(self) -> list[RequestResult]:
        results: list[RequestResult] = []
        for i in range(1, self._config.runs + 1):
            size, elapsed = self._measure()
            results.append(self._emit(i, size, elapsed))
        return results

    def _run_parallel(self) -> list[RequestResult]:
        total = self._config.runs
        workers = min(self._config.connections, total)
        results: list[RequestResult] = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._measure) for _ in range(total)]
            try:
                for i, fut in enumerate(as_completed(futures), start=1):
                    size, elapsed = fut.result()
                    results.append(self._emit(i, size, elapsed))
            except BaseException:
                for f in futures:
                    f.cancel()
                raise
        return results


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #

def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="HTTP download speed tester")
    p.add_argument("url", help="URL to download (large static file)")
    p.add_argument("-n", "--runs", type=int, default=10,
                   help="total number of requests (default: 10)")
    p.add_argument("-t", "--timeout", type=float, default=60.0,
                   help="per-request timeout, seconds (default: 60)")
    p.add_argument("-c", "--connections", type=int, default=1,
                   help="max concurrent connections (default: 1 = sequential)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON report")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = TestConfig(
            url=args.url,
            runs=args.runs,
            timeout=args.timeout,
            connections=args.connections,
        )
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2

    reporter: Reporter = JsonReporter() if args.json else ConsoleReporter()
    test = SpeedTest(config=config, fetcher=UrllibFetcher(), reporter=reporter)

    try:
        test.run()
    except FetchError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
