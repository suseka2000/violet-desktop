"""Bounded subprocess entry points for large DXF work; no HTTP/global limit changes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore
import time

from backend.importing.errors import ImportSourceError, MalformedSourceError, SourceLimitError
from .desktop_runtime import worker_command, worker_launch_options, track_worker, windows_resident_bytes

MAX_INPUT_BYTES = 64 * 1024**2
MAX_OUTPUT_BYTES = 16 * 1024**2
TIMEOUT_SECONDS = 75
MAX_RSS_BYTES = 1536 * 1024**2
SLOTS = BoundedSemaphore(2)


def _resident_bytes(pid):
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
        pass
    return None


def _run(operation, content, parameters):
    if not isinstance(content, bytes):
        raise TypeError("Исходник должен быть передан байтами")
    if not content or len(content) > MAX_INPUT_BYTES:
        raise SourceLimitError("DXF должен быть непустым и не превышать 64 МиБ")
    if not SLOTS.acquire(blocking=False):
        raise SourceLimitError("Заняты оба обработчика больших DXF. Дождитесь завершения текущей проверки.")
    try:
        with tempfile.TemporaryDirectory(prefix="violet-dxf-workspace-") as temporary:
            output = Path(temporary) / "result.json"
            errors = Path(temporary) / "errors.log"
            source_path = Path(temporary) / "source.dxf"
            source_path.write_bytes(content)
            with source_path.open("rb") as source, output.open("w+b") as stdout, errors.open("w+b") as stderr:
                try:
                    process = track_worker(subprocess.Popen(worker_command("backend.dxf_workspace_worker", operation,
                        json.dumps(parameters, ensure_ascii=True, allow_nan=False)), stdin=source, stdout=stdout, stderr=stderr,
                        cwd=str(Path(__file__).resolve().parents[1]), **worker_launch_options()))
                except OSError as exc:
                    raise SourceLimitError("Не удалось запустить обработчик DXF") from exc
                deadline = time.monotonic() + TIMEOUT_SECONDS
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise SourceLimitError(f"Обработка DXF превысила {TIMEOUT_SECONDS} секунд. Выберите меньше слоёв или сохраните меньший фрагмент.")
                        if os.fstat(stdout.fileno()).st_size > MAX_OUTPUT_BYTES or os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
                            raise SourceLimitError("Обработчик DXF превысил лимит результата 16 МиБ")
                        resident = _resident_bytes(process.pid)
                        if resident is not None and resident > MAX_RSS_BYTES:
                            raise SourceLimitError("Обработка DXF превысила лимит памяти 1,5 ГиБ")
                        time.sleep(.1)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                if process.returncode or os.fstat(stdout.fileno()).st_size > MAX_OUTPUT_BYTES or os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise SourceLimitError("Обработчик DXF не уложился в допустимые пределы ресурсов")
                stdout.seek(0)
                try:
                    envelope = json.loads(stdout.read(MAX_OUTPUT_BYTES+1), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                except (ValueError, UnicodeError) as exc:
                    raise MalformedSourceError("Некорректный ответ обработчика DXF") from exc
            if not isinstance(envelope, dict) or ("error" not in envelope and not isinstance(envelope.get("result"), dict)):
                raise MalformedSourceError("Некорректный ответ обработчика DXF")
            if "error" in envelope:
                error = ImportSourceError(envelope["error"]["message"], details=envelope["error"].get("details"))
                error.code = envelope["error"]["code"]
                raise error
            return envelope["result"]
    finally:
        SLOTS.release()


def run_dxf_inspect(content: bytes):
    return _run("inspect", content, {})


def run_dxf_view(content: bytes, layout=1, region=None, layers=None):
    from backend.dxf_workspace import validate_view_parameters
    validate_view_parameters(layout, region, layers)
    return _run("view", content, {"layout": layout, "region": region, "layers": layers})


def main():
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (65, 66))
            # ezdxf needs a bounded temporary input file as well as JSON output.
            # JSON is separately capped below and checked by the parent.
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_INPUT_BYTES + 1, MAX_INPUT_BYTES + 1))
            if sys.platform.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        except (ImportError, OSError, ValueError):
            pass
        from backend.dxf_workspace import inspect_dxf_workspace, view_dxf_workspace
        content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(content) > MAX_INPUT_BYTES:
            raise SourceLimitError("DXF превышает 64 МиБ")
        operation = sys.argv[1]
        parameters = json.loads(sys.argv[2])
        if operation == "inspect":
            result = inspect_dxf_workspace(content)
        elif operation == "view":
            result = view_dxf_workspace(content, **parameters)
        else:
            raise ValueError("Неизвестная операция")
        envelope = {"result": result}
    except ImportSourceError as exc:
        envelope = {"error": exc.as_dict()}
    except Exception:
        envelope = {"error": {"code": "DXF_WORKSPACE_FAILED", "message": "Не удалось безопасно обработать DXF", "details": {}}}
    encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps({"error": {"code": "SOURCE_LIMIT_EXCEEDED", "message": "Слишком много данных для одного вида DXF", "details": {}}}).encode()
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
