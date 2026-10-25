#!/usr/bin/env python3
"""
scripts/simulate_load.py - Concurrent Load Generation & Queue Drainage Monitor
=============================================================================
Simulates concurrent online judge submissions to trigger PostgreSQL queue
accumulation, observe KEDA autoscaling of judge workers, and monitor queue
drainage in real time.

Usage:
    python3 scripts/simulate_load.py --count 30 --api-url http://localhost:8080/api
    python3 scripts/simulate_load.py --count 50 --concurrency 15
    python3 scripts/simulate_load.py --mode run --count 20

CLI Arguments:
    --count         Number of submissions/jobs to queue (default: 30)
    --api-url       Base REST API URL (default: http://localhost:8080/api)
    --username      Admin/User username (default: admin)
    --password      Admin/User password (default: AdminPass123!)
    --concurrency   Concurrent HTTP client workers (default: 10)
    --mode          Job generation mode: 'submit' (problem solution) or 'run' (ad-hoc run) (default: submit)
    --problem-id    Target problem ID when mode=submit (default: 1)
    --watch         Monitor queue drainage and worker scaling until backlog drains (default: True)
    --no-watch      Disable queue drainage monitoring loop
    --watch-interval Seconds between queue polling checks (default: 2.0)
"""

import argparse
import asyncio
import math
import re
import sys
import time
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    print("Error: 'httpx' is required. Install with: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ANSI formatting helpers
USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def green(t: str) -> str:
    return color(t, "32")


def cyan(t: str) -> str:
    return color(t, "36")


def yellow(t: str) -> str:
    return color(t, "33")


def red(t: str) -> str:
    return color(t, "31")


def bold(t: str) -> str:
    return color(t, "1")


SAMPLE_PYTHON_CODE = """# Auto-generated load test solution
import sys
import time

time.sleep(0.3)  # Predictable workload to simulate sandbox compute
lines = sys.stdin.read().split()
if len(lines) >= 2:
    print(int(lines[0]) + int(lines[1]))
else:
    print(0)
"""


class LoadSimulator:
    def __init__(
        self,
        api_url: str,
        count: int,
        concurrency: int,
        username: str,
        password: str,
        mode: str,
        problem_id: int,
        watch: bool,
        watch_interval: float,
    ):
        self.api_url = api_url.rstrip("/")
        self.count = count
        self.concurrency = concurrency
        self.username = username
        self.password = password
        self.mode = mode
        self.problem_id = problem_id
        self.watch = watch
        self.watch_interval = watch_interval

        # Auth tokens: list of (token, user_id)
        self.tokens: List[str] = []
        self.job_ids: List[str] = []
        self.is_admin: bool = False

    async def authenticate(self, client: httpx.AsyncClient) -> None:
        """Authenticate as admin or create virtual load test accounts."""
        print(f"{bold('[1/3]')} Authenticating with API at {cyan(self.api_url)}...")

        # 1. Try specified username/password
        try:
            resp = await client.post(
                f"{self.api_url}/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data["access_token"]
                self.tokens.append(token)

                # Check if admin (admins are exempt from MAX_ACTIVE_JOBS=5 limit)
                me_resp = await client.get(
                    f"{self.api_url}/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if me_resp.status_code == 200 and me_resp.json().get("is_admin"):
                    self.is_admin = True
                    print(
                        f"  {green('✓')} Authenticated as {bold(self.username)} (Admin - unlimited concurrency)."
                    )
                    return
                print(
                    f"  {yellow('!')} Authenticated as non-admin user {bold(self.username)}."
                )
        except Exception as exc:
            print(f"  {yellow('!')} Direct login failed ({exc}), provisioning virtual test accounts...")

        # 2. If not admin or login failed, provision pool of virtual accounts
        # Each non-admin user can have up to 4 concurrent active jobs
        needed_users = max(1, math.ceil(self.count / 4))
        print(
            f"  {cyan('ℹ')} Provisioning {needed_users} virtual load-test users to bypass per-user job caps..."
        )

        for i in range(needed_users):
            uname = f"loadtest_user_{i}_{int(time.time()) % 10000}"
            pword = "TestPass123!"
            email = f"{uname}@example.local"

            # Register
            reg_resp = await client.post(
                f"{self.api_url}/auth/register",
                json={"username": uname, "email": email, "password": pword},
                timeout=10.0,
            )
            if reg_resp.status_code in (201, 409):
                # Login
                login_resp = await client.post(
                    f"{self.api_url}/auth/login",
                    json={"username": uname, "password": pword},
                    timeout=10.0,
                )
                if login_resp.status_code == 200:
                    self.tokens.append(login_resp.json()["access_token"])

        if not self.tokens:
            raise RuntimeError(
                "Failed to authenticate or create virtual load test accounts!"
            )

        print(
            f"  {green('✓')} Successfully initialized auth token pool ({len(self.tokens)} active users)."
        )

    async def _submit_single(
        self,
        client: httpx.AsyncClient,
        index: int,
        semaphore: asyncio.Semaphore,
        stats: Dict[str, Any],
    ) -> None:
        token = self.tokens[index % len(self.tokens)]
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        t_start = time.monotonic()
        async with semaphore:
            try:
                if self.mode == "submit":
                    payload = {
                        "problem_id": self.problem_id,
                        "language": "python",
                        "src_code": SAMPLE_PYTHON_CODE,
                    }
                    url = f"{self.api_url}/submit"
                else:
                    payload = {
                        "language": "python",
                        "src_code": SAMPLE_PYTHON_CODE,
                        "std_in": "10 20\n",
                    }
                    url = f"{self.api_url}/run"

                resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
                elapsed_ms = (time.monotonic() - t_start) * 1000

                if resp.status_code == 202:
                    data = resp.json()
                    job_id = data.get("job_id") or data.get("run_id") or "queued"
                    self.job_ids.append(job_id)
                    stats["success"] += 1
                    status_str = green(f"202 Accepted (job={job_id[:8]}...)")
                else:
                    stats["failed"] += 1
                    status_str = red(f"HTTP {resp.status_code} ({resp.text[:30]})")

                print(
                    f"  [{index + 1:2d}/{self.count}] {status_str} in {elapsed_ms:5.1f}ms"
                )

            except Exception as e:
                stats["failed"] += 1
                print(f"  [{index + 1:2d}/{self.count}] {red(f'ERROR: {str(e)[:40]}')}")

    async def generate_load(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        """Dispatch concurrent submission requests."""
        print(
            f"\n{bold('[2/3]')} Generating {bold(str(self.count))} jobs (concurrency={self.concurrency}, mode={self.mode})..."
        )
        semaphore = asyncio.Semaphore(self.concurrency)
        stats = {"success": 0, "failed": 0}

        t0 = time.monotonic()
        tasks = [
            self._submit_single(client, i, semaphore, stats) for i in range(self.count)
        ]
        await asyncio.gather(*tasks)
        total_time = time.monotonic() - t0

        stats["total_time"] = total_time
        stats["throughput"] = self.count / total_time if total_time > 0 else 0

        print(
            f"\n{bold('Injection Complete:')} {green(str(stats['success']))} succeeded, "
            f"{red(str(stats['failed']))} failed in {total_time:.2f}s "
            f"({stats['throughput']:.1f} submissions/sec)."
        )
        return stats

    async def fetch_queue_depth(self, client: httpx.AsyncClient) -> Optional[Dict[str, int]]:
        """Scrape Prometheus metrics endpoint to inspect judge_queue_depth."""
        # Try both direct backend /metrics and nginx /api/metrics
        metrics_urls = [
            f"{self.api_url}/metrics",
            f"{self.api_url.replace('/api', '')}/metrics",
            "http://localhost:9000/metrics",
            "http://localhost:8080/api/metrics",
        ]
        for url in metrics_urls:
            try:
                resp = await client.get(url, timeout=3.0)
                if resp.status_code == 200 and "judge_queue_depth" in resp.text:
                    depths: Dict[str, int] = {"queued": 0, "running": 0, "done": 0}
                    for match in re.finditer(
                        r'judge_queue_depth\{status="([a-zA-Z0-9_]+)"\}\s+([0-9.]+)',
                        resp.text,
                    ):
                        st, val = match.group(1), int(float(match.group(2)))
                        depths[st] = val
                    return depths
            except Exception:
                continue
        return None

    async def monitor_queue_drain(self, client: httpx.AsyncClient) -> None:
        """Poll metrics to observe KEDA scale-out and queue drainage in real-time."""
        print(
            f"\n{bold('[3/3]')} Monitoring queue saturation, KEDA target replicas, and drain..."
        )
        print("  Press Ctrl+C at any time to exit.\n")

        t_start = time.monotonic()
        peak_queue = 0
        last_queued = -1

        while True:
            elapsed = time.monotonic() - t_start
            depths = await self.fetch_queue_depth(client)

            if depths is not None:
                queued = depths.get("queued", 0)
                running = depths.get("running", 0)
                peak_queue = max(peak_queue, queued)

                # KEDA autoscaling formula:
                # Replicas = ceil(queued / targetQueryValue=5), clamped between 1 and 10
                if queued == 0 and running == 0:
                    keda_replicas = 1
                else:
                    keda_replicas = min(10, max(1, math.ceil(queued / 5.0)))

                # Visual queue progress bar
                bar_len = 20
                ratio = queued / peak_queue if peak_queue > 0 else 0
                filled = int(ratio * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)

                status_line = (
                    f"  [{elapsed:5.1f}s] "
                    f"Queued: {yellow(str(queued)):>4} | "
                    f"Running: {cyan(str(running)):>2} | "
                    f"KEDA Target Replicas: {green(str(keda_replicas)):>2} | "
                    f"[{bar}]"
                )
                print(status_line)

                if queued == 0 and running == 0 and elapsed > 3.0:
                    print(
                        f"\n{green('✓')} {bold('Queue fully drained!')} All jobs processed in {elapsed:.1f}s."
                    )
                    break

                last_queued = queued
            else:
                print(
                    f"  [{elapsed:5.1f}s] Metrics endpoint not responding yet; waiting for jobs to process..."
                )

            await asyncio.sleep(self.watch_interval)

    async def run(self) -> None:
        limits = httpx.Limits(max_keepalive_connections=self.concurrency, max_connections=self.concurrency * 2)
        async with httpx.AsyncClient(limits=limits, follow_redirects=True) as client:
            await self.authenticate(client)
            await self.generate_load(client)

            if self.watch:
                try:
                    await self.monitor_queue_drain(client)
                except KeyboardInterrupt:
                    print(f"\n{yellow('!')} Monitoring stopped by user.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="YuktiCode Concurrent Load Simulator & KEDA Scaler Monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=30,
        help="Number of submissions to enqueue",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default="http://localhost:8080/api",
        help="Base REST API URL (e.g. http://localhost:8080/api or http://localhost:9000)",
    )
    parser.add_argument(
        "--username",
        type=str,
        default="admin",
        help="Admin username for unthrottled load testing",
    )
    parser.add_argument(
        "--password",
        type=str,
        default="AdminPass123!",
        help="Password for authentication",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent HTTP client workers",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["submit", "run"],
        default="submit",
        help="Submission endpoint: 'submit' (problem 1) or 'run' (ad-hoc code run)",
    )
    parser.add_argument(
        "--problem-id",
        type=int,
        default=1,
        help="Target problem ID when mode=submit",
    )
    parser.add_argument(
        "--watch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Watch real-time queue depth and KEDA worker scaling until empty",
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=2.0,
        help="Interval in seconds between queue polling checks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    simulator = LoadSimulator(
        api_url=args.api_url,
        count=args.count,
        concurrency=args.concurrency,
        username=args.username,
        password=args.password,
        mode=args.mode,
        problem_id=args.problem_id,
        watch=args.watch,
        watch_interval=args.watch_interval,
    )
    try:
        asyncio.run(simulator.run())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
