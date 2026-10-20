"""Bound HTTP parser work in child processes, with no external file paths.

Local limits protect responsiveness, not a remote multi-tenant sandbox. Native
decoder security still requires dependency updates and OS-level isolation.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from threading import BoundedSemaphore
from .desktop_runtime import worker_command, worker_launch_options

SLOTS = BoundedSemaphore(2)


def _run(operation, content, parameters):
    from .importing import ImportSourceError
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Исходник превышает 8 МиБ")
    if not SLOTS.acquire(blocking=False):
        raise ValueError("Заняты оба обработчика исходников. Повторите запрос после завершения текущей проверки.")
    try:
        payload = json.dumps({"operation": operation, "content": base64.b64encode(content).decode(), "parameters": parameters}).encode()
        try:
            result = subprocess.run(worker_command("backend.source_worker"), input=payload,
                                    capture_output=True, timeout=30, check=False, **worker_launch_options())
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Обработка исходника превысила 30 секунд. Выделите меньший фрагмент чертежа.") from exc
        if result.returncode or len(result.stdout) > 64 * 1024 * 1024:
            raise ValueError("Обработчик не смог прочитать исходник в допустимых пределах ресурсов")
        envelope = json.loads(result.stdout)
        if "error" in envelope:
            error = ImportSourceError(envelope["error"]["message"], details=envelope["error"].get("details"))
            error.code = envelope["error"]["code"]
            raise error
        return envelope["result"]
    finally:
        SLOTS.release()


def inspect_isolated(filename, content, units_override=None):
    return _run("inspect", content, {"filename": filename, "units_override": units_override})


def convert_isolated(content, **parameters):
    from .importing import ImportResult
    return ImportResult.model_validate(_run("convert", content, parameters))


def main():
    from .importing import ImportSourceError, inspect_source, convert_cad_source
    try:
        # CPU limit complements the parent wall-clock timeout. Not all platforms
        # expose this; the parent deadline and semaphore remain active there.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (25, 26))
        except (ImportError, OSError, ValueError):
            pass
        raw = sys.stdin.buffer.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("Запрос слишком большой")
        request = json.loads(raw)
        content = base64.b64decode(request["content"], validate=True)
        if request["operation"] == "inspect":
            result = inspect_source(content=content, **request["parameters"])
        elif request["operation"] == "convert":
            result = convert_cad_source(content, **request["parameters"]).model_dump(mode="json")
        else:
            raise ValueError("Неподдерживаемая операция")
        response = {"result": result}
    except ImportSourceError as exc:
        response = {"error": exc.as_dict()}
    except Exception:
        response = {"error": {"code": "SOURCE_PARSE_FAILED", "message": "Не удалось безопасно разобрать исходник", "details": {}}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
