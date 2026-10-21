"""Small Docker judge.

One fresh, read-only container per test case. The source and stdin are copied
into a tmpfs; the expected output never enters the container. The fixed script
inside the image compiles and runs the program, and writes small result files.
"""

import logging
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound
from docker.types import Ulimit

from server.config import (
    JUDGE_IMAGE,
    JUDGE_MAX_OUTPUT_BYTES,
    JUDGE_RUNTIME,
)

logger = logging.getLogger(__name__)

_SOURCE_FILES = {"py": "main.py", "cpp": "main.cpp", "java": "Main.java"}
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _dockerfile_path() -> Path:
    return Path(__file__).resolve().parent / "judger_image" / "Dockerfile"


def ensure_image(client) -> str:
    """Build the judge image once if it is missing."""
    try:
        client.images.get(JUDGE_IMAGE)
        return JUDGE_IMAGE
    except ImageNotFound:
        logger.info("Building judge image %s (first run only)", JUDGE_IMAGE)
        client.images.build(
            path=str(_dockerfile_path().parent),
            tag=JUDGE_IMAGE,
            rm=True,
            forcerm=True,
        )
        return JUDGE_IMAGE


def _runtime() -> str | None:
    configured = (JUDGE_RUNTIME or "auto").strip().lower()
    if configured not in ("", "auto", "default", "runc"):
        return configured
    try:
        if "runsc" in _get_client().info().get("Runtimes", {}):
            return "runsc"
    except DockerException:
        pass
    return None


def _files(source: str, stdin: str, language: str) -> list[tuple[str, bytes]]:
    """Exactly the two files the candidate should see, never expected output."""
    return [
        (_SOURCE_FILES[language], (source or "").encode("utf-8")),
        ("input.txt", (stdin or "").encode("utf-8")),
    ]


def _write_file(container, path: str, data: bytes) -> None:
    """Write a file through an exec stdin stream.

    `put_archive` is rejected by Docker when the rootfs is read-only, even
    when the target is a tmpfs. A one-shot `cat > file` exec is the smallest
    safe way to populate the writable tmpfs without relaxing read_only.
    """
    exec_id = container.client.api.exec_create(
        container.id,
        ["/bin/sh", "-c", 'cat > "$1"', "sh", path],
        stdin=True,
        stdout=False,
        stderr=False,
    )["Id"]
    stream = container.client.api.exec_start(exec_id, socket=True)
    try:
        stream._sock.sendall(data)
        stream._sock.shutdown(socket.SHUT_WR)
    finally:
        stream.close()

    deadline = time.monotonic() + 5.0
    while True:
        info = container.client.api.exec_inspect(exec_id)
        if not info["Running"]:
            if info["ExitCode"] != 0:
                raise RuntimeError(f"could not write {path}: exit {info['ExitCode']}")
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out writing {path}")
        time.sleep(0.01)


def _read_bytes(container, path: str, limit: int) -> tuple[str, bool]:
    """Read a capped container file through exec.

    Docker also rejects get_archive when the rootfs is read-only, so ask the
    container's own `head` for only limit + 1 bytes. A larger result means the
    file exceeded the cap.
    """
    try:
        result = container.exec_run(
            ["/usr/bin/head", "-c", str(limit + 1), path],
            demux=True,
        )
        stdout = result.output[0] if result.output else b""
        data = stdout or b""
        return data[:limit].decode("utf-8", errors="replace"), len(data) > limit
    except Exception as exc:  # missing file or daemon error
        logger.debug("could not read %s: %s", path, exc)
        return "", False


def _read_int(container, path: str, default: int = 0) -> int:
    text, _ = _read_bytes(container, path, 64)
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return default


def compare_outputs(expected: str, actual: str) -> bool:
    def lines(text: str):
        values = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        start, end = 0, len(values) - 1
        while start <= end and values[start].strip() == "":
            start += 1
        while end >= start and values[end].strip() == "":
            end -= 1
        for index in range(start, end + 1):
            yield values[index].rstrip()

    return list(lines(expected)) == list(lines(actual))


def _parse_metrics(container) -> tuple[float, float]:
    text, _ = _read_bytes(container, "/workspace/time.txt", 512)
    match = re.search(r"MEM:(\d+)\s+CPU:([0-9.]+)\+([0-9.]+)", text)
    if not match:
        return 0.0, 0.0
    memory_mb = float(match.group(1)) / 1024.0
    cpu_ms = (float(match.group(2)) + float(match.group(3))) * 1000.0
    return memory_mb, cpu_ms


def run_case(
    *,
    language: str,
    source: str,
    stdin: str,
    expected: str | None,
    time_limit_ms: int,
    memory_limit_mb: int,
) -> dict:
    """Run one test case in a fresh container and return a verdict dict."""
    client = _get_client()
    image = ensure_image(client)
    container = None
    started = time.perf_counter()

    kwargs = {
        "image": image,
        "command": ["sleep", "infinity"],
        "detach": True,
        "network_mode": "none",
        "read_only": True,
        "user": "65534:65534",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": 64,
        "mem_limit": f"{memory_limit_mb}m",
        "memswap_limit": f"{memory_limit_mb}m",
        "nano_cpus": 1_000_000_000,
        "ulimits": [
            Ulimit(name="nofile", soft=256, hard=256),
            # Docker passes RLIMIT_FSIZE in bytes (the shell's `ulimit -f`
            # uses 512-byte blocks, so run-judge repeats the same 2 MiB cap).
            Ulimit(name="fsize", soft=2 * 1024 * 1024, hard=2 * 1024 * 1024),
        ],
        "tmpfs": {
            "/workspace": "rw,nosuid,nodev,exec,mode=1777,size=64m",
            "/tmp": "rw,nosuid,nodev,mode=1777,size=64m",
            "/dev/shm": "rw,nosuid,nodev,noexec,mode=1777,size=32m",
        },
        "environment": {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
        "working_dir": "/workspace",
        "labels": {"cfclone.judge": "1"},
    }
    runtime = _runtime()
    if runtime:
        kwargs["runtime"] = runtime

    try:
        container = client.containers.create(**kwargs)
        container.start()
        for name, data in _files(source, stdin, language):
            _write_file(container, f"/workspace/{name}", data)
        container.exec_run(
            [
                "/usr/local/bin/run-judge",
                language,
                f"{max(0.1, time_limit_ms / 1000.0):.3f}",
                str(memory_limit_mb),
            ],
            workdir="/workspace",
        )

        compile_code = _read_int(container, "/workspace/compile_code", 0)
        compile_output, compile_large = _read_bytes(
            container, "/workspace/compile_output.txt", JUDGE_MAX_OUTPUT_BYTES
        )
        if compile_code != 0:
            message = compile_output
            if compile_large:
                message += "\n[compile output truncated]"
            return {
                "status": "CE",
                "execution_time_ms": 0.0,
                "peak_memory_mb": 0.0,
                "message": message[:4000],
                "std_out": "",
            }

        exit_code = _read_int(container, "/workspace/exit_code", 1)
        stdout, stdout_large = _read_bytes(
            container, "/workspace/stdout.txt", JUDGE_MAX_OUTPUT_BYTES
        )
        stderr, _ = _read_bytes(
            container, "/workspace/stderr.txt", JUDGE_MAX_OUTPUT_BYTES
        )
        memory_mb, cpu_ms = _parse_metrics(container)
        wall_ms = (time.perf_counter() - started) * 1000.0

        def result(status: str, message: str = "", output: str = "") -> dict:
            return {
                "status": status,
                "execution_time_ms": round(cpu_ms or wall_ms, 2),
                "peak_memory_mb": round(memory_mb, 2),
                "message": message[:4000],
                "std_out": output,
            }

        if exit_code in (124, 143):
            return result(
                "TLE",
                f"time limit exceeded ({time_limit_ms} ms)",
            )
        if exit_code == 137:
            return result("MLE", f"memory limit exceeded ({memory_limit_mb} MB)")
        if exit_code != 0:
            return result("RE", stderr or f"exit code {exit_code}", stdout)

        if stdout_large:
            return result("RE", "output exceeded the output limit", stdout)

        if expected is not None and not compare_outputs(expected, stdout):
            return result("WA", "output did not match", stdout)

        return result("AC", "", stdout)

    except DockerException as exc:
        logger.exception("Docker failure while judging")
        return {
            "status": "SYSTEM_ERROR",
            "execution_time_ms": 0.0,
            "peak_memory_mb": 0.0,
            "message": f"judge infrastructure error: {exc}"[:4000],
            "std_out": "",
        }
    except Exception as exc:
        logger.exception("Unexpected judge failure")
        return {
            "status": "SYSTEM_ERROR",
            "execution_time_ms": 0.0,
            "peak_memory_mb": 0.0,
            "message": f"judge error: {exc}"[:4000],
            "std_out": "",
        }
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.warning("could not remove judge container", exc_info=True)


def run_submit(
    *,
    language: str,
    source: str,
    test_cases: list[dict],
    time_limit_ms: int,
    memory_limit_mb: int,
) -> dict:
    if not test_cases:
        return {
            "status": "SYSTEM_ERROR",
            "execution_time_ms": 0.0,
            "peak_memory_mb": 0.0,
            "message": "problem has no test cases",
        }

    total_ms = 0.0
    peak_mb = 0.0
    for index, test_case in enumerate(test_cases, start=1):
        result = run_case(
            language=language,
            source=source,
            stdin=test_case.get("input", ""),
            expected=test_case.get("expected_output", ""),
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
        )
        total_ms += float(result.get("execution_time_ms", 0.0))
        peak_mb = max(peak_mb, float(result.get("peak_memory_mb", 0.0)))
        if result["status"] != "AC":
            result["execution_time_ms"] = round(total_ms, 2)
            result["peak_memory_mb"] = round(peak_mb, 2)
            result["message"] = f"test {index}: {result.get('message', '')}".strip()
            return result

    return {
        "status": "AC",
        "execution_time_ms": round(total_ms, 2),
        "peak_memory_mb": round(peak_mb, 2),
        "message": "",
    }


def run_custom(
    *,
    language: str,
    source: str,
    stdin: str,
    expected: str | None,
    time_limit_ms: int,
    memory_limit_mb: int,
) -> dict:
    result = run_case(
        language=language,
        source=source,
        stdin=stdin,
        expected=expected,
        time_limit_ms=time_limit_ms,
        memory_limit_mb=memory_limit_mb,
    )
    return {
        "status": result["status"],
        "std_out": result.get("std_out", ""),
        "message": result.get("message", ""),
        "execution_time_ms": result.get("execution_time_ms", 0.0),
        "peak_memory_mb": result.get("peak_memory_mb", 0.0),
    }


def run_batch(
    *,
    language: str,
    source: str,
    tests: list[dict],
    time_limit_ms: int,
    memory_limit_mb: int,
) -> dict:
    results = []
    for index, test_case in enumerate(tests):
        expected = test_case.get("expected_output")
        result = run_custom(
            language=language,
            source=source,
            stdin=test_case.get("input", " "),
            expected=expected,
            time_limit_ms=time_limit_ms,
            memory_limit_mb=memory_limit_mb,
        )
        results.append({"test_index": index, **result})
    return {"tests": results}


def reap_orphaned_containers(max_age_minutes: int = 20) -> int:
    """Remove leftover judge containers after a worker crash."""
    client = _get_client()
    cutoff = time.time() - max_age_minutes * 60
    removed = 0
    try:
        containers = client.containers.list(
            all=True, filters={"label": "cfclone.judge=1"}
        )
    except DockerException:
        logger.warning("could not list judge containers", exc_info=True)
        return 0

    for container in containers:
        try:
            created = container.attrs.get("Created", "")
            # Docker timestamps look like 2024-03-20T12:00:00.000000000Z.
            parsed = created.split(".")[0].replace("Z", "")
            epoch = datetime.strptime(parsed, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
            if epoch < cutoff:
                container.remove(force=True)
                removed += 1
        except Exception:
            logger.debug("failed to inspect/reap %s", container, exc_info=True)
    return removed
