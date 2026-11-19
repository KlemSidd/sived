"""On-disk agent packages.

An editable package lives at ``{DATA_DIR}/agents/{persona_id}/``.
Checkout copies it to ``{DATA_DIR}/jobs/{job_id}/agent/``.
The worker stages a run at ``{DATA_DIR}/jobs/{job_id}/work/``.

A package is one text file or hundreds. ``manifest.json`` names ``workflow.json``.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from app import config
from app.agent import PLATFORM_SKILLS

MAX_FILES = 400
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 8
_ID = re.compile(r"^[0-9a-f-]{36}$")


def check_id(value: str, label: str = "package") -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} not found")
    return value


def persona_dir(persona_id: str) -> Path:
    return config.DATA_DIR / "agents" / check_id(persona_id, "persona")


def job_agent_dir(job_id: str) -> Path:
    return config.DATA_DIR / "jobs" / check_id(job_id, "workload") / "agent"


def job_work_dir(job_id: str) -> Path:
    return config.DATA_DIR / "jobs" / check_id(job_id, "workload") / "work"


def safe_rel(value: str) -> str:
    """A relative path that cannot leave the package."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a package path is required")
    if "\\" in value or value.startswith("/") or value.startswith("~"):
        raise ValueError("a package path must stay inside the agent")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("a package path must stay inside the agent")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("a package path must stay inside the agent")
    if len(parts) > MAX_DEPTH:
        raise ValueError("a package path is too deep")
    for part in parts:
        if part != part.strip() or len(part) > 80:
            raise ValueError("a package path must stay inside the agent")
    return "/".join(parts)


def list_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob("*"):
        if path.is_file():
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def read_tree(root: Path) -> dict[str, str]:
    files = {}
    for rel in list_files(root):
        data = (root / rel).read_bytes()
        if b"\x00" in data:
            raise ValueError(f"{rel} must be text")
        try:
            files[safe_rel(rel)] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{rel} must be text") from exc
    return files


def files_from_zip(blob: bytes) -> dict[str, str]:
    if not isinstance(blob, (bytes, bytearray)):
        raise ValueError("upload a package zip")
    if len(blob) > MAX_TOTAL_BYTES:
        raise ValueError("a package is over 8 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("upload a package zip") from exc
    files: dict[str, str] = {}
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        rel = safe_rel(info.filename)
        if info.file_size > MAX_FILE_BYTES:
            raise ValueError(f"{rel} is over 200,000 bytes")
        data = archive.read(info)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{rel} is over 200,000 bytes")
        if b"\x00" in data:
            raise ValueError(f"{rel} must be text")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{rel} must be text") from exc
        files[rel] = text
        total += len(data)
        if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("a package can have 400 files and 8 MB")
    if not files:
        raise ValueError("package is empty")
    return files


def prepare_tree(root: Path, files: dict[str, str]) -> Path:
    """Write files beside root. The live package stays put until adopt_tree."""
    checked = _checked(files)
    root.parent.mkdir(parents=True, exist_ok=True)
    incoming = root.parent / f".{root.name}.incoming"
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir()
    try:
        _write_files(incoming, checked)
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    return incoming


def adopt_tree(root: Path, incoming: Path) -> Path | None:
    """Make incoming the live package. Returns the previous tree, if there was one."""
    return _swap(root, incoming)


def replace_tree(root: Path, files: dict[str, str]) -> Path | None:
    """Replace root with these files. Returns the previous tree, if there was one."""
    incoming = prepare_tree(root, files)
    try:
        return adopt_tree(root, incoming)
    except Exception:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise


def discard_backup(backup: Path | None) -> None:
    if backup is not None and backup.exists():
        shutil.rmtree(backup)


def restore_backup(root: Path, backup: Path | None) -> None:
    if root.exists():
        shutil.rmtree(root)
    if backup is not None and backup.exists():
        backup.rename(root)


def ensure_persona_package(persona) -> Path:
    """Materialize workflow.json when this persona has no package yet."""
    root = persona_dir(persona["id"])
    if (root / "manifest.json").is_file():
        return root
    from app.harness import starter_files

    files, _code = starter_files()
    backup = replace_tree(root, files)
    discard_backup(backup)
    return root


def ensure_job_package(job) -> Path:
    """Use the workflow.json copied at checkout."""
    root = job_agent_dir(job["id"])
    if (root / "manifest.json").is_file() and (root / "workflow.json").is_file():
        return root
    raise ValueError("the persona has no workflow.json")


def snapshot_persona(persona_id: str, job_id: str) -> Path:
    source = persona_dir(persona_id)
    if not (source / "manifest.json").is_file():
        raise ValueError("the persona has no package")
    dest = job_agent_dir(job_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    return dest


def custom_skills(root: Path) -> list[dict]:
    """Skill bodies shipped in the package. Built-in skill names are not files."""
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return []
    found = []
    for child in sorted(skills_root.iterdir()):
        body_path = child / "SKILL.md"
        if not child.is_dir() or not body_path.is_file():
            continue
        if child.name in PLATFORM_SKILLS:
            continue
        found.append({"name": child.name, "body": body_path.read_text(encoding="utf-8")})
    return found


def _checked(files: dict[str, str]) -> dict[str, str]:
    if len(files) > MAX_FILES:
        raise ValueError("a package can have 400 files and 8 MB")
    checked = {}
    total = 0
    for rel, text in files.items():
        rel = safe_rel(rel)
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError(f"{rel} must be text")
        try:
            data = text.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{rel} must be text") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{rel} is over 200,000 bytes")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("a package can have 400 files and 8 MB")
        checked[rel] = text
    if not checked:
        raise ValueError("package is empty")
    return checked


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        dest = root.joinpath(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def _swap(root: Path, incoming: Path) -> Path | None:
    backup = root.parent / f".{root.name}.backup"
    if backup.exists():
        shutil.rmtree(backup)
    had = root.exists()
    if had:
        root.rename(backup)
    try:
        incoming.rename(root)
    except Exception:
        if had and backup.exists() and not root.exists():
            backup.rename(root)
        raise
    if had:
        return backup
    return None
