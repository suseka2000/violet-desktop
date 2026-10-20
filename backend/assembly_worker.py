"""Multi-source native assembly worker with bounded private binary transport.

Resource containment, not an OS security sandbox. Sources cannot specify paths
or external files. The native slot is shared with ordinary STEP work to avoid
simultaneous memory-heavy kernels in one application process.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
from .desktop_runtime import worker_command, worker_launch_options, track_worker
import tempfile
import time

from .assembly_geometry import MAX_INPUT_BYTES, MAX_SOURCES, build_assembly, validate_assembly_sources, validate_assembly_spec
from .step_geometry import StepGeometryError
from .step_worker import SLOTS, MAX_RSS_BYTES, _resident_bytes

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
WORKER_TIMEOUT_SECONDS = 90


def _json(raw):
    def reject_constant(value):
        raise ValueError("Non-finite JSON")
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


def _read_request(stream):
    """Length-framed bytes; no base64 amplification and no caller file paths."""
    raw = stream.readline(MAX_HEADER_BYTES + 1)
    if len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\n"):
        raise StepGeometryError("Заголовок сборки превышает лимит", code="ASSEMBLY_LIMIT")
    try:
        header = _json(raw)
    except (ValueError, UnicodeError) as exc:
        raise StepGeometryError("Некорректный заголовок сборки", code="ASSEMBLY_INVALID") from exc
    if not isinstance(header, dict) or set(header) != {"spec", "sources"} or not isinstance(header["sources"], list) or len(header["sources"]) > MAX_SOURCES:
        raise StepGeometryError("Некорректный список исходных STEP", code="ASSEMBLY_INVALID")
    spec = validate_assembly_spec(header["spec"])
    sources, total = {}, 0
    for item in header["sources"]:
        if not isinstance(item, dict) or set(item) != {"source_id", "byte_length"} or not isinstance(item["source_id"], str) or item["source_id"] in sources:
            raise StepGeometryError("Некорректное описание исходного STEP", code="ASSEMBLY_INVALID")
        size = item["byte_length"]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_INPUT_BYTES or total + size > MAX_INPUT_BYTES:
            raise StepGeometryError("Общий размер исходных STEP превышает 64 МиБ", code="ASSEMBLY_LIMIT")
        total += size
        content = stream.read(size)
        if len(content) != size:
            raise StepGeometryError("Передача исходного STEP прервана", code="ASSEMBLY_INVALID")
        sources[item["source_id"]] = content
    if stream.read(1):
        raise StepGeometryError("Лишние данные после исходных STEP", code="ASSEMBLY_INVALID")
    validate_assembly_sources(sources, spec)
    return sources, spec


def run_assembly_build(sources: dict[str, bytes], spec: dict, *, timeout_seconds=None) -> dict:
    if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 90 or not math.isfinite(timeout_seconds)):
        raise StepGeometryError("Некорректный лимит времени сборки", code="ASSEMBLY_LIMIT")
    timeout = WORKER_TIMEOUT_SECONDS if timeout_seconds is None else min(WORKER_TIMEOUT_SECONDS, timeout_seconds)
    spec = validate_assembly_spec(spec)
    validate_assembly_sources(sources, spec)
    header = json.dumps({"spec": spec, "sources": [{"source_id": key, "byte_length": len(sources[key])} for key in sorted(sources)]}, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(header) > MAX_HEADER_BYTES:
        raise StepGeometryError("Заголовок сборки превышает лимит", code="ASSEMBLY_LIMIT")
    if not SLOTS.acquire(blocking=False):
        raise StepGeometryError("Обработчик STEP занят. Дождитесь завершения текущего расчёта.", code="STEP_BUSY")
    try:
        with tempfile.TemporaryDirectory(prefix="interior-assembly-worker-") as directory:
            scratch = Path(directory)
            with (scratch / "input").open("wb") as source:
                source.write(header)
                for key in sorted(sources):
                    source.write(sources[key])
            with (scratch / "input").open("rb") as source, (scratch / "result").open("w+b") as output, (scratch / "log").open("w+b") as log:
                try:
                    process = track_worker(subprocess.Popen(worker_command("backend.assembly_worker"), stdin=source, stdout=output, stderr=log, cwd=str(Path(__file__).resolve().parents[1]), **worker_launch_options()))
                except OSError as exc:
                    raise StepGeometryError("Не удалось запустить обработчик сборки", code="STEP_WORKER_FAILED") from exc
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise StepGeometryError(f"Расчёт сборки превысил {timeout:g} секунд. Выберите меньше деталей или упростите модель.", code="STEP_TIMEOUT")
                        if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES or os.fstat(log.fileno()).st_size > MAX_OUTPUT_BYTES:
                            raise StepGeometryError("Результат сборки превышает лимит 16 МиБ", code="ASSEMBLY_LIMIT")
                        rss = _resident_bytes(process.pid)
                        if rss is not None and rss > MAX_RSS_BYTES:
                            raise StepGeometryError("Расчёт сборки превысил лимит памяти 1,5 ГиБ", code="ASSEMBLY_LIMIT")
                        time.sleep(.1)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                if process.returncode or os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES or os.fstat(log.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise StepGeometryError("Обработчик сборки завершился вне допустимых ресурсов", code="STEP_WORKER_FAILED")
                output.seek(0)
                try:
                    envelope = _json(output.read(MAX_OUTPUT_BYTES + 1))
                except (ValueError, UnicodeError) as exc:
                    raise StepGeometryError("Обработчик сборки вернул некорректный результат", code="STEP_WORKER_FAILED") from exc
                if not isinstance(envelope, dict) or len(envelope) != 1:
                    raise StepGeometryError("Некорректный ответ обработчика сборки", code="STEP_WORKER_FAILED")
                if "error" in envelope:
                    error = envelope["error"]
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str) or not isinstance(error.get("code"), str) or not isinstance(error.get("details", {}), dict):
                        raise StepGeometryError("Некорректная ошибка обработчика сборки", code="STEP_WORKER_FAILED")
                    raise StepGeometryError(error["message"], code=error["code"], details=error.get("details"))
                if not isinstance(envelope.get("result"), dict) or envelope["result"].get("schema") != "assembled-section-build/1":
                    raise StepGeometryError("Пустой результат сборки", code="STEP_WORKER_FAILED")
                return envelope["result"]
    finally:
        SLOTS.release()


def main():
    response_fd = os.dup(1)
    os.dup2(2, 1)  # Keep native OCCT diagnostics out of structured response.
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (80, 85))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        except (ImportError, OSError, ValueError):
            pass
        if len(sys.argv) != 1:
            raise StepGeometryError("Обработчик сборки не принимает пути или параметры запуска", code="ASSEMBLY_INVALID")
        sources, spec = _read_request(sys.stdin.buffer)
        envelope = {"result": build_assembly(sources, spec)}
    except StepGeometryError as exc:
        envelope = {"error": exc.as_dict()}
    except Exception:
        envelope = {"error": {"code": "ASSEMBLY_BUILD_FAILED", "message": "Не удалось безопасно построить сборку. Проверьте исходные модели и плоскость сечения.", "details": {}}}
    raw = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = json.dumps({"error": {"code": "ASSEMBLY_LIMIT", "message": "Результат сборки превышает лимит 16 МиБ", "details": {}}}).encode()
    with os.fdopen(response_fd, "wb") as output:
        output.write(raw)


if __name__ == "__main__":
    main()
