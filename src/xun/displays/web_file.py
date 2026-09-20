"""File browsing and transfer API for the web display.

All routes are scoped under ``/api/files/{agent_id}`` and serve files from the
agent's workdir only. Symlinks and path components are resolved and re-checked
against the workdir so that neither traversal nor symlink escapes can reach
files outside the agent's workspace.

This module is decoupled from the web display itself: routes resolve agents
through an ``agent_getter`` callback instead of importing the display, and the
zip packing runs on a thread pool so it never blocks the event loop.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import stat
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import puremagic

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

if TYPE_CHECKING:
    from ..agent import Agent

AgentGetter = Callable[[str], "Agent[Agent.T.Any]"]
"""Resolves an agent identifier to an Agent, raising HTTPException(404) when unknown."""

class CreateDirectory(BaseModel):
    path: str

class MovePath(BaseModel):
    path: str
    destination: str

class DeletePath(BaseModel):
    path: str

# Media types the stdlib table misses, guesses wrong, or reports as
# non-previewable for plain-text source/config files.
MEDIA_TYPE_OVERRIDES = {
    ".vue": "text/x-vue",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".go": "text/go",
    ".rs": "text/rust",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".sh": "text/x-sh",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".properties": "text/plain",
    ".env": "text/plain",
    ".bat": "text/plain",
    ".cmd": "text/plain",
    ".ps1": "text/plain",
    ".psm1": "text/plain",
    ".vbs": "text/plain",
    ".vbe": "text/plain",
    ".bib": "text/x-bibtex",
    ".ris": "text/plain",
    ".tex": "text/x-tex",
    ".sty": "text/x-tex",
    ".cls": "text/x-tex",
    ".dtx": "text/x-tex",
    ".ltx": "text/x-tex",
}

# Structured text formats that preview as text although they are not text/*.
TEXT_MEDIA_TYPES = {"application/json", "application/toml", "application/xml", "application/yaml"}

# Inline previews serve agent/user content: nosniff + a null CSP keep SVG or
# mislabelled files from running scripts or masquerading as another type.
INLINE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
}

MAX_ARCHIVE_SIZE = 1_073_741_824  # 1 GiB hard cap on the packed archive
MAX_PREVIEW_TEXT_SIZE = 1_000_000  # 1 MB cap on inline text previews
MAX_PREVIEW_IMAGE_SIZE = 20_971_520  # 20 MiB cap on inline image previews
MAX_PREVIEW_DOCUMENT_SIZE = 50_000_000  # 50 MB cap on inline document previews
CHUNK_SIZE = 1024 * 1024
_SLASH_NAME = re.compile(r"[/\\]")


def resolve_path(agent: "Agent[Agent.T.Any]", relative_path: str, *, follow_symlinks: bool = True) -> Path:
    """Resolve ``relative_path`` against the agent workdir and reject escapes."""
    root = agent.workspace.workdir.expanduser().resolve()
    target = Path(os.path.abspath(root / relative_path))
    if target != root and root not in target.parents:
        raise HTTPException(400, "Path escapes the agent workdir")
    if follow_symlinks:
        target = target.resolve()
        if target != root and root not in target.parents:
            raise HTTPException(400, "Path escapes the agent workdir")
    return target


def resolve_entry_path(agent: "Agent[Agent.T.Any]", relative_path: str) -> Path:
    """Resolve and validate parent directories without following the final symlink."""
    root = agent.workspace.workdir.expanduser().resolve()
    target = resolve_path(agent, relative_path, follow_symlinks=False)
    if target == root:
        return target
    parent = resolve_path(agent, target.parent.relative_to(root).as_posix())
    return parent / target.name


@lru_cache(maxsize=4096)
def _sniff_mime(path_str: str, mtime_ns: int, size: int) -> str:
    """Content sniff cached by path+mtime+size so repeat listings skip the reads."""
    try:
        if size == 0:
            # puremagic raises PureValueError on empty input; empty files have
            # nothing to sniff and are best treated as plain text.
            return "text/plain"
        return puremagic.from_file(path_str, mime=True)
    except (puremagic.PureError, ValueError, OSError) as exc:
        # PureValueError subclasses ValueError, not PureError, in puremagic 2.x.
        print(f"Warning: could not sniff media type for {path_str}: {exc}. Falling back to application/octet-stream.")
        return "application/octet-stream"

def _media_type(path: Path, file_stat: os.stat_result | None = None) -> str:
    """Return an extension guess, falling back to a cached content sniff."""
    guess = MEDIA_TYPE_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if guess:
        return guess
    try:
        file_stat = file_stat or path.stat()
    except OSError as exc:
        print(f"Warning: could not stat {path}: {exc}. Falling back to application/octet-stream.")
        return "application/octet-stream"
    return _sniff_mime(str(path), file_stat.st_mtime_ns, file_stat.st_size)


def _slugify(path: str) -> str:
    """Name for the download; the empty path (the workdir root) becomes "workspace"."""
    name = path.strip("/").split("/")[-1] or "workspace"
    name = _SLASH_NAME.sub("_", name).strip()
    return name or "workspace"


def _make_archive(agent: "Agent[Agent.T.Any]", path: str) -> tuple[str, str]:
    """Synchronously pack a file or folder in the workdir into a temp zip.

    Runs on a thread pool (via ``run_in_threadpool``) so large archives do not
    block the event loop. Returns ``(temp_zip_path, download_name)``; the
    caller owns the temp file.
    """
    target = resolve_path(agent, path)
    if not target.exists():
        raise HTTPException(404, "Path not found")
    slug = _slugify(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f"xun-archive-{slug}-", suffix=".zip")
    os.close(fd)
    try:
        total = 0
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            if target.is_dir():
                # prefix entries with the folder name so extraction lands in a single directory
                prefix = f"{target.name}/"
                files = sorted(target.rglob("*"), key=lambda file: file.relative_to(target).as_posix())
                files = [file for file in files if file.is_file() and not file.is_symlink()]
                base = target
            else:
                prefix = ""
                files = [target]
                base = target.parent
            for item in files:
                info = zipfile.ZipInfo(prefix + item.relative_to(base).as_posix())
                info.external_attr = (item.stat().st_mode & 0xFFFF) << 16
                archive.writestr(info, item.read_bytes())
                total += item.stat().st_size
                if total > MAX_ARCHIVE_SIZE:
                    raise HTTPException(413, "Folder is too large to archive")
        return tmp_name, f"{slug}.zip"
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def build_file_router(agent_getter: AgentGetter) -> APIRouter:
    """Build the file API router.

    ``agent_getter`` maps an agent id (from the URL path) to an Agent and is
    the only hook this module needs on the host application.
    """
    router = APIRouter()

    @router.get("/api/files/{agent_id}")
    def list_files(agent_id: str, path: str = "") -> dict:
        # Sync on purpose: FastAPI runs the blocking directory walk on the
        # thread pool instead of stalling the event loop on large folders.
        agent = agent_getter(agent_id)
        target = resolve_path(agent, path)
        if not target.is_dir():
            raise HTTPException(404, "Directory not found")
        root = agent.workspace.workdir.resolve()
        # DirEntry reuses type information from the directory scan on most
        # filesystems, avoiding a stat call per item.
        with os.scandir(target) as scan:
            children = [entry for entry in scan if not entry.is_symlink()]
        children_with_kind = [(entry, entry.is_dir()) for entry in children]
        children_with_kind.sort(key=lambda item: (not item[1], item[0].name.lower()))
        entries = [
            {
                "name": entry.name,
                "path": Path(entry.path).relative_to(root).as_posix(),
                "kind": "directory" if is_directory else "file",
            }
            for entry, is_directory in children_with_kind
        ]
        return {"path": path, "entries": entries}

    @router.get("/api/files/{agent_id}/info")
    def file_info(agent_id: str, path: str) -> dict:
        target = resolve_path(agent_getter(agent_id), path)
        try:
            file_stat = target.stat()
        except OSError as exc:
            raise HTTPException(404, "Path not found") from exc
        is_directory = stat.S_ISDIR(file_stat.st_mode)
        return {
            "name": target.name,
            "path": path,
            "kind": "directory" if is_directory else "file",
            "size": None if is_directory else file_stat.st_size,
            "media_type": None if is_directory else _media_type(target, file_stat),
            "modified_at": file_stat.st_mtime,
        }

    @router.get("/api/files/{agent_id}/content")
    async def file_content(agent_id: str, path: str) -> Response:
        """Serve file bytes for the preview pane; new formats only extend this dispatch."""
        target = resolve_path(agent_getter(agent_id), path)
        if not target.is_file():
            raise HTTPException(404, "File not found")
        media_type = _media_type(target)
        if media_type.startswith("image/"):
            if target.stat().st_size > MAX_PREVIEW_IMAGE_SIZE:
                raise HTTPException(413, "Image is too large to preview")
            return FileResponse(target, media_type=media_type, headers=INLINE_SECURITY_HEADERS)
        if media_type == "application/pdf":
            if target.stat().st_size > MAX_PREVIEW_DOCUMENT_SIZE:
                raise HTTPException(413, "Document is too large to preview")
            return FileResponse(target, media_type=media_type, headers=INLINE_SECURITY_HEADERS)
        if media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES:
            if target.stat().st_size > MAX_PREVIEW_TEXT_SIZE:
                raise HTTPException(413, "File is too large to preview")
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(415, "File is not UTF-8 text") from exc
            return Response(content=text, media_type=f"{media_type}; charset=utf-8", headers=INLINE_SECURITY_HEADERS)
        raise HTTPException(415, "File cannot be previewed")

    @router.get("/api/files/{agent_id}/download")
    async def download_file(agent_id: str, path: str) -> FileResponse:
        target = resolve_path(agent_getter(agent_id), path)
        if not target.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(target, media_type=_media_type(target), filename=target.name)

    @router.get("/api/files/{agent_id}/archive")
    async def download_archive(agent_id: str, path: str = "") -> FileResponse:
        """Pack a file or folder in the workdir into a temporary zip and stream it."""
        agent = agent_getter(agent_id)
        tmp_name, download_name = await run_in_threadpool(_make_archive, agent, path)
        return FileResponse(
            tmp_name,
            media_type="application/zip",
            filename=download_name,
            background=BackgroundTask(Path(tmp_name).unlink, missing_ok=True),
        )

    @router.post("/api/files/{agent_id}/upload")
    async def upload_files(
        agent_id: str,
        files: list[UploadFile] = File(...),
        path: str = Form(default=""),
    ) -> dict[str, list[str]]:
        agent = agent_getter(agent_id)
        directory = resolve_path(agent, path)
        if not directory.is_dir():
            raise HTTPException(404, "Directory not found")
        uploaded = []
        for upload in files:
            name = Path(upload.filename or "").name
            if not name:
                continue
            target = resolve_path(agent, str(Path(path) / name))
            with target.open("wb") as output:
                while chunk := await upload.read(CHUNK_SIZE):
                    output.write(chunk)
            uploaded.append(name)
        return {"uploaded": uploaded}

    def entry(agent_id: str, path: str) -> tuple["Agent[Agent.T.Any]", Path, Path]:
        agent = agent_getter(agent_id)
        root = agent.workspace.workdir.resolve()
        return agent, resolve_entry_path(agent, path), root

    @router.post("/api/files/{agent_id}/create-directory")
    async def create_directory(agent_id: str, request: CreateDirectory) -> dict[str, str]:
        _agent, target, root = entry(agent_id, request.path)
        if target == root:
            raise HTTPException(400, "Cannot modify the workdir")
        if target.exists() or target.is_symlink():
            raise HTTPException(409, "Path already exists")
        if not target.parent.is_dir():
            raise HTTPException(404, "Parent directory not found")
        target.mkdir()
        return {"path": target.relative_to(root).as_posix()}

    @router.post("/api/files/{agent_id}/move")
    async def move_path(agent_id: str, request: MovePath) -> dict[str, str]:
        agent, target, root = entry(agent_id, request.path)
        if target == root:
            raise HTTPException(400, "Cannot modify the workdir")
        if not target.exists() and not target.is_symlink():
            raise HTTPException(404, "Path not found")
        destination = resolve_entry_path(agent, request.destination)
        if destination == root:
            raise HTTPException(400, "Cannot replace the workdir")
        if destination.exists() or destination.is_symlink():
            raise HTTPException(409, "Destination already exists")
        if not destination.parent.is_dir():
            raise HTTPException(404, "Destination directory not found")
        if target.is_dir() and target in destination.parents:
            raise HTTPException(400, "Cannot move a directory into itself")
        try:
            target.rename(destination)
        except OSError as exc:
            raise HTTPException(400, "Could not move path") from exc
        return {"path": destination.relative_to(root).as_posix()}

    @router.post("/api/files/{agent_id}/delete")
    async def delete_path(agent_id: str, request: DeletePath) -> dict[str, bool]:
        _agent, target, root = entry(agent_id, request.path)
        if target == root:
            raise HTTPException(400, "Cannot delete the workdir")
        if target.is_file() or target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            raise HTTPException(404, "Path not found")
        return {"deleted": True}

    return router
