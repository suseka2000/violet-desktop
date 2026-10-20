"""Isolated STEP native parser with wall/CPU/RSS/output and concurrency bounds.

This is resource containment, not an OS security sandbox. No caller-supplied
paths or executable choices cross the API. CAD dependencies still need updates.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore
import time

from .step_geometry import StepGeometryError, MAX_STEP_BYTES, validate_step_content, validate_section_parameters
from .desktop_runtime import worker_command, worker_launch_options, track_worker, windows_resident_bytes

# Shared by all native STEP/assembly subprocesses. Two independent, individually
# bounded workers keep long walls from serializing an entire floor, while still
# bounding combined native RSS (at most two workers, not one per wall).
SLOTS = BoundedSemaphore(2)
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RSS_BYTES = 1536 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 90


def _resident_bytes(pid: int) -> int | None:
    """Supervise resident memory on Linux/macOS (RLIMIT_AS alone is not portable)."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        elif sys.platform == "darwin":
            status = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, timeout=1, check=False)
            if status.returncode == 0 and status.stdout.strip():
                return int(status.stdout.strip()) * 1024
        elif sys.platform == "win32":
            return windows_resident_bytes(pid)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def _run(operation: str, content: bytes, parameters: dict, *, timeout_seconds=None) -> dict:
    if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 90):
        raise StepGeometryError("Некорректный лимит времени STEP", code="STEP_LIMIT")
    timeout = WORKER_TIMEOUT_SECONDS if timeout_seconds is None else min(WORKER_TIMEOUT_SECONDS, timeout_seconds)
    validate_step_content(content)
    if not SLOTS.acquire(blocking=False):
        raise StepGeometryError("Обработчик STEP занят. Дождитесь завершения текущего сечения.", code="STEP_BUSY")
    try:
        # Files are private, fixed-name scratch resources, deleted on every exit.
        # Unlike capture_output, disk-backed stdout cannot exhaust parent RAM.
        with tempfile.TemporaryDirectory(prefix="interior-step-worker-") as directory:
            scratch = Path(directory)
            source_path = scratch / "input"
            source_path.write_bytes(content)
            with source_path.open("rb") as source, (scratch / "result").open("w+b") as output, (scratch / "log").open("w+b") as log:
                command = worker_command("backend.step_worker", operation, json.dumps(parameters, allow_nan=False))
                try:
                    process = track_worker(subprocess.Popen(command, stdin=source, stdout=output, stderr=log, cwd=str(Path(__file__).resolve().parents[1]), **worker_launch_options()))
                except OSError as exc:
                    raise StepGeometryError("Не удалось запустить локальный обработчик STEP", code="STEP_WORKER_FAILED") from exc
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise StepGeometryError(f"Обработка STEP превысила {timeout:g} секунд. Выберите отдельный узел или упростите модель.", code="STEP_TIMEOUT")
                        if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES or os.fstat(log.fileno()).st_size > MAX_OUTPUT_BYTES:
                            raise StepGeometryError("Результат STEP превышает лимит 8 МиБ", code="STEP_LIMIT")
                        rss = _resident_bytes(process.pid)
                        if rss is not None and rss > MAX_RSS_BYTES:
                            raise StepGeometryError("Обработка STEP превысила лимит памяти 1,5 ГиБ", code="STEP_LIMIT")
                        time.sleep(0.1)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                if process.returncode or os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise StepGeometryError("Обработчик STEP завершился вне допустимых ресурсов или не смог прочитать модель", code="STEP_WORKER_FAILED")
                output.seek(0)
                try:
                    envelope = json.loads(output.read(MAX_OUTPUT_BYTES + 1), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                    raise StepGeometryError("Обработчик STEP вернул некорректный результат", code="STEP_WORKER_FAILED") from exc
                if not isinstance(envelope, dict):
                    raise StepGeometryError("Обработчик STEP вернул некорректный результат", code="STEP_WORKER_FAILED")
                if "error" in envelope:
                    error = envelope["error"]
                    raise StepGeometryError(error["message"], code=error["code"], details=error.get("details"))
                if not isinstance(envelope.get("result"), dict):
                    raise StepGeometryError("Пустой результат STEP", code="STEP_WORKER_FAILED")
                return envelope["result"]
    finally:
        SLOTS.release()


def run_step_inspect(content: bytes, *, timeout_seconds=None) -> dict:
    return _run("inspect", content, {}, timeout_seconds=timeout_seconds)


def run_step_section(content: bytes, axis: str, offset_mm: float, component_ids: list[int] | None = None, *, timeout_seconds=None) -> dict:
    axis, offset_mm, component_ids = validate_section_parameters(axis, offset_mm, component_ids)
    return _run("section", content, {"axis": axis, "offset_mm": offset_mm, "component_ids": component_ids}, timeout_seconds=timeout_seconds)


def main():
    response_fd = os.dup(1)
    os.dup2(2, 1)  # Native OCCT diagnostics cannot corrupt the JSON transport.
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (80, 85))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        except (ImportError, OSError, ValueError):
            pass
        from .step_geometry import inspect_step, section_step
        content = sys.stdin.buffer.read(MAX_STEP_BYTES + 1)
        if len(sys.argv) != 3:
            raise StepGeometryError("Некорректный запрос STEP")
        parameters = json.loads(sys.argv[2])
        if sys.argv[1] == "inspect":
            result = inspect_step(content)
        elif sys.argv[1] == "section":
            result = section_step(content, **parameters)
        else:
            raise StepGeometryError("Неизвестная операция STEP")
        envelope = {"result": result}
    except StepGeometryError as exc:
        envelope = {"error": exc.as_dict()}
    except Exception:
        envelope = {"error": {"code": "STEP_PARSE_FAILED", "message": "Не удалось безопасно обработать STEP. Проверьте модель в исходной CAD-системе.", "details": {}}}
    raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = json.dumps({"error": {"code": "STEP_LIMIT", "message": "Результат STEP превышает лимит 8 МиБ", "details": {}}}).encode()
    with os.fdopen(response_fd, "wb") as output:
        output.write(raw)


if __name__ == "__main__":
    main()
