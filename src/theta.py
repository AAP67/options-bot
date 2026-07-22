"""Theta Terminal lifecycle + query client.

ThetaData is not a plain HTTP API. You run their Java program locally, it
authenticates against ThetaData with your API key, and *it* serves data on
localhost. So before any option data can be fetched, a process has to be
launched, proven healthy, and later torn down — especially in CI, where a
stray process outlives the job.

`ThetaTerminal` owns that lifecycle:

    with ThetaTerminal() as theta:
        expirations = theta.get("/v3/option/list/expirations", symbol="RIOT")

Verified against the live v3 Terminal on 2026-07-22:
  * serves on port 25503 (not 25510 — that was v2)
  * all `/v2/*` paths return 410; v3 paths are `/v3/<asset>/<group>/<op>`
  * strikes are in dollars ("25.000"), `right` is `call`/`put`/`both`
  * a FREE key returns option EOD with bid/ask back to 2023-06-01, but
    open_interest needs VALUE and implied volatility/Greeks need STANDARD
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_JAR = REPO_ROOT / "vendor" / "ThetaTerminalv3.jar"
DEFAULT_PORT = 25503

# The jar is a bootstrap: it contacts ThetaData, resolves the right Terminal
# build, and downloads ~98MB of libraries before serving anything. Measured at
# 141s on a cold Codespace. The timeout has to cover that or CI fails on its
# first ever run and looks like a broken key.
STARTUP_TIMEOUT = 300.0
POLL_INTERVAL = 2.0

# FREE keys allow 20 requests/minute and 1 concurrent request. Spacing requests
# is cheaper than handling throttle errors mid-backfill, where a year of EOD
# data across several tickers is thousands of calls. Operational, not strategy
# — deliberately not in rules_vN.yaml.
MIN_REQUEST_INTERVAL = 3.0

# Readiness probe. Deliberately a real data request rather than a bare port
# check: the HTTP server accepts connections before it has authenticated
# upstream, so a socket test would report "ready" while every query fails.
# This path is small and always present for a liquid symbol.
HEALTH_PATH = "/v3/option/list/expirations"
HEALTH_PARAMS = {"symbol": "AAPL", "format": "json"}

# The Terminal answers subscription failures with 200 and a plain-text body.
# Left undetected they look like empty results — the silent-wrong-data failure
# iron rule #4 exists to prevent.
_SUBSCRIPTION_MARKERS = ("subscription", "consider upgrading")
_NO_DATA_MARKER = "no data found"


class ThetaError(RuntimeError):
    """Terminal could not be started, reached, or trusted."""


class ThetaSubscriptionError(ThetaError):
    """The endpoint exists but the current plan does not include it."""


class ThetaTerminal:
    """Manages the Theta Terminal process and queries it over HTTP."""

    def __init__(
        self,
        jar_path: pathlib.Path | None = None,
        port: int = DEFAULT_PORT,
        startup_timeout: float = STARTUP_TIMEOUT,
        request_interval: float = MIN_REQUEST_INTERVAL,
    ) -> None:
        self.jar_path = jar_path or DEFAULT_JAR
        self.port = port
        self.startup_timeout = startup_timeout
        self.request_interval = request_interval
        self._process: subprocess.Popen[bytes] | None = None
        self._log: Any = None
        self._owns_process = False
        self._last_request = 0.0

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> ThetaTerminal:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_up(self) -> bool:
        """True when the Terminal answers a real data request."""
        try:
            self._request(HEALTH_PATH, HEALTH_PARAMS, timeout=5.0)
        except ThetaSubscriptionError:
            return True  # reachable and authenticated; plan simply lacks this
        except Exception:
            return False
        return True

    def start(self) -> None:
        """Launch the Terminal, or attach to one already running on the port.

        Attaching matters for local development, where a Terminal is often left
        running: a second instance would fail to bind and look like a crash.
        """
        if self.is_up():
            self._owns_process = False
            return

        if not self.jar_path.exists():
            raise ThetaError(
                f"Theta Terminal jar not found at {self.jar_path}. Download it: "
                "curl -sSL -o vendor/ThetaTerminalv3.jar "
                "https://downloads.thetadata.us/ThetaTerminalv3.jar"
            )
        if not os.environ.get("THETADATA_API_KEY", "").strip():
            raise ThetaError("THETADATA_API_KEY is not set (see .env.example).")

        # Output goes to a file, not PIPE: nobody drains a pipe during the
        # startup poll, and a full pipe buffer would deadlock the process.
        self._log = tempfile.NamedTemporaryFile(
            prefix="theta-terminal-", suffix=".log", delete=False
        )
        self._process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            ["java", "-jar", str(self.jar_path)],
            stdout=self._log,
            stderr=subprocess.STDOUT,
            cwd=str(self.jar_path.parent),
        )
        self._owns_process = True

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.stop()
                raise ThetaError(
                    f"Theta Terminal exited during startup: {self._tail_log()}"
                )
            if self.is_up():
                return
            time.sleep(POLL_INTERVAL)

        self.stop()
        raise ThetaError(
            f"Theta Terminal not ready after {self.startup_timeout:.0f}s: "
            f"{self._tail_log()}"
        )

    def stop(self) -> None:
        """Terminate the process if we started it. Safe to call twice.

        Never skipped: an orphaned Terminal in CI holds the job open until the
        runner's own timeout.
        """
        process, self._process = self._process, None
        if process is not None and self._owns_process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if self._log is not None:
            self._log.close()
            self._log = None
        self._owns_process = False

    def _tail_log(self, lines: int = 8) -> str:
        """Last few log lines, for putting a cause in the error message."""
        if self._log is None:
            return "(no log)"
        try:
            text = pathlib.Path(self._log.name).read_text(errors="replace")
        except OSError:
            return "(log unreadable)"
        return " | ".join(text.strip().splitlines()[-lines:]) or "(no output)"

    # -- queries ------------------------------------------------------------

    def _throttle(self) -> None:
        """Space requests to stay under the plan's per-minute limit."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request = time.monotonic()

    def _request(
        self,
        path: str,
        params: dict[str, Any],
        timeout: float = 60.0,
    ) -> str:
        """Raw GET returning the response body as text."""
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            # Verified live: a plan-gated endpoint answers 403 with the reason
            # in the body, not 200-with-text. Classify it so callers can tell
            # "you cannot have this" from "the Terminal is broken".
            if any(marker in detail.lower() for marker in _SUBSCRIPTION_MARKERS):
                raise ThetaSubscriptionError(f"{path}: {detail.strip()}") from exc
            raise ThetaError(f"{path} returned HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise ThetaError(f"could not reach Theta Terminal at {url}: {exc}") from exc

        lowered = body[:400].lower()
        if any(marker in lowered for marker in _SUBSCRIPTION_MARKERS):
            raise ThetaSubscriptionError(f"{path}: {body.strip()[:200]}")
        return body

    def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Query a v3 endpoint and return its `response` list.

        Returns [] when the Terminal reports no data — a genuinely empty
        result (a contract with no trades that day) rather than an error. A
        *blocked* endpoint raises ThetaSubscriptionError instead, so a missing
        subscription can never be mistaken for an empty market.
        """
        params.setdefault("format", "json")
        self._throttle()
        body = self._request(path, params)

        if _NO_DATA_MARKER in body[:200].lower():
            return []
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ThetaError(f"{path}: expected JSON, got {body[:200]!r}") from exc
        return payload.get("response", [])
