"""Command line entry point, including the local server."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .domain.builders import make_demo_project
from .domain.models import Project
from .geometry.validation import validate_project
from .service import InvalidProjectError, ProjectService
from .storage.repository import Repository, StorageError
from .storage.json_codec import load_project, write_project

ROOT = Path(__file__).resolve().parent.parent


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Основа генератора рабочей документации")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "projects.sqlite3", help="База проектов SQLite")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Запустить локальный интерфейс и API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8310, type=int)
    sub.add_parser("list", help="Список сохранённых проектов")
    create = sub.add_parser("create", help="Создать пустой проект")
    create.add_argument("name")
    create.add_argument("--author", default="")
    create.add_argument("--address", default="")
    demo = sub.add_parser("demo", help="Создать пример с двумя этажами")
    demo.add_argument("--output", type=Path, help="Записать JSON вместо сохранения в базу")
    demo.add_argument("--detailed", action="store_true", help="Расширенный учебный комплект с зонами на двух этажах")
    validate = sub.add_parser("validate", help="Проверить файл без изменения базы")
    validate.add_argument("path", type=Path)
    imp = sub.add_parser("import", help="Импортировать JSON как новый проект")
    imp.add_argument("path", type=Path)
    exp = sub.add_parser("export", help="Экспортировать проект")
    exp.add_argument("project_id")
    exp.add_argument("path", type=Path)
    exp.add_argument("--revision", type=int)
    exp.add_argument("--overwrite", action="store_true")
    hist = sub.add_parser("history", help="История версий проекта")
    hist.add_argument("project_id")
    restore = sub.add_parser("restore", help="Восстановить старое состояние как новую версию")
    restore.add_argument("project_id")
    restore.add_argument("source_revision", type=int)
    restore.add_argument("--expected-revision", required=True, type=int)
    backup = sub.add_parser("backup", help="Сделать согласованную резервную копию SQLite")
    backup.add_argument("path", type=Path)
    workspace_backup = sub.add_parser("backup-workspace", help="Полная резервная копия базы, истории и исходников")
    workspace_backup.add_argument("path", type=Path)
    workspace_backup.add_argument("--sources", type=Path, help="Каталог исходников (по умолчанию sources рядом с базой)")
    verify_backup = sub.add_parser("verify-backup", help="Проверить полный архив без восстановления")
    verify_backup.add_argument("path", type=Path)
    restore_workspace = sub.add_parser("restore-workspace", help="Восстановить полный архив только в новую директорию")
    restore_workspace.add_argument("path", type=Path)
    restore_workspace.add_argument("target", type=Path)
    schema = sub.add_parser("schema", help="Показать JSON Schema модели")
    schema.add_argument("--output", type=Path)
    documentation = sub.add_parser("document", help="Альбом PDF/DXF или комплект ZIP из JSON-файла")
    documentation.add_argument("project_file", type=Path)
    documentation.add_argument("output", type=Path)
    documentation.add_argument("--format", choices=["ALBUM", "A3", "A2"], default="ALBUM", help="ALBUM: обложка A3 и графические листы A2; A3/A2: полный комплект")
    engineering = sub.add_parser("engineering", help="Расчёт развёрток, раскладки и открытых вопросов из JSON")
    engineering.add_argument("project_file", type=Path)
    inspect = sub.add_parser("inspect-source", help="Проверка исходника DXF/PDF/PNG/JPEG без изменения проекта")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--units", dest="units_override")
    reference = sub.add_parser("inspect-reference-dxf", help="Потоковая проверка большого DXF рабочей документации без создания помещений (до 256 МиБ)")
    reference.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    repository = None
    try:
        if args.command == "inspect-reference-dxf":
            from .reference_drawing import inspect_reference_dxf
            emit(inspect_reference_dxf(args.path))
            return 0
        if args.command in {"verify-backup", "restore-workspace"}:
            from .storage.workspace_archive import validate_workspace_archive, restore_workspace
            result = validate_workspace_archive(args.path) if args.command == "verify-backup" else restore_workspace(args.path, args.target)
            emit(result)
            return 0
        if args.command == "inspect-source":
            from .importing import inspect_source
            if args.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Исходник превышает лимит 8 МБ")
            emit(inspect_source(args.path.name, args.path.read_bytes(), args.units_override))
            return 0
        if args.command in {"document", "engineering"}:
            from .service import validated
            project = validated(load_project(args.project_file))
            if args.command == "engineering":
                from .engineering import build_engineering
                emit(build_engineering(project, catalog=project.properties.get("node_catalog")))
                return 0
            from .documents import build_documents
            if args.output.suffix.lower() not in {".pdf", ".dxf", ".zip"}:
                raise ValueError("Выходной файл должен иметь расширение .pdf, .dxf или .zip")
            if args.output.exists():
                raise FileExistsError("Файл уже существует; выберите новое имя")
            documents = build_documents(project, paper_format=args.format)
            content = {".zip": documents.package, ".pdf": documents.pdf, ".dxf": documents.dxf}[args.output.suffix.lower()]()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("xb") as output:
                output.write(content)
            emit({"path": str(args.output.resolve()), "sheets": len(documents.sheets), "status": "draft_not_for_construction", "readiness": documents.report["readiness"]})
            return 0
        if args.command == "validate":
            from .workspace import validate_workspace
            validate_workspace(load_project(args.path))
            report = validate_project(load_project(args.path))
            emit(report.model_dump(mode="json"))
            return 0 if report.valid else 2
        if args.command == "schema":
            data = Project.model_json_schema()
            data["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation: regenerating requires an explicit destination.
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                print(args.output)
            else:
                emit(data)
            return 0
        if args.command == "demo" and args.output:
            from .demo import make_documentation_demo
            project = make_documentation_demo() if args.detailed else make_demo_project()
            write_project(project, args.output)
            print(args.output)
            return 0
        if args.command == "serve":
            from .api.server import build_server
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
            server = build_server(args.db, host=args.host, port=args.port)
            print(f"Открыть в браузере: http://{args.host}:{server.server_address[1]}", flush=True)
            print(f"База проектов: {args.db}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        repository = Repository(args.db)
        service = ProjectService(repository)
        if args.command == "list":
            emit(repository.list_projects())
        elif args.command == "create":
            project = service.create(args.name, args.address, args.author)
            emit({"id": project.id, "name": project.metadata.name, "revision": project.revision})
        elif args.command == "demo":
            from .demo import make_documentation_demo
            project = service.save(make_documentation_demo(), expected_revision=0, message="Расширенный учебный пример") if args.detailed else service.demo()
            emit({"id": project.id, "revision": project.revision, "validation": validate_project(project).model_dump(mode="json")})
        elif args.command == "import":
            project = service.import_copy(load_project(args.path))
            emit({"id": project.id, "revision": project.revision})
        elif args.command == "export":
            write_project(repository.get(args.project_id, revision=args.revision), args.path, overwrite=args.overwrite)
            print(args.path)
        elif args.command == "history":
            emit(repository.history(args.project_id))
        elif args.command == "restore":
            project = service.restore(args.project_id, args.source_revision, args.expected_revision)
            emit({"id": project.id, "revision": project.revision})
        elif args.command == "backup":
            repository.backup(args.path)
            print(args.path)
        elif args.command == "backup-workspace":
            from .importing import SourceStore
            from .storage.workspace_archive import backup_workspace
            from .importing._common import MAX_CAD_SOURCE_BYTES
            source_store = SourceStore(args.sources or args.db.parent / "sources", scope="local", max_bytes=MAX_CAD_SOURCE_BYTES)
            emit(backup_workspace(repository, source_store, args.path))
        return 0
    except InvalidProjectError as error:
        emit(error.report.model_dump(mode="json"))
        return 2
    except (ValidationError, ValueError, OSError, StorageError, sqlite3.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    finally:
        if repository is not None:
            repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
