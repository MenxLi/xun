from __future__ import annotations

import fnmatch
import hashlib
import os
import secrets
import socket
import sys
import tarfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Protocol, cast

import docker
from docker.client import DockerClient
from docker.errors import NotFound
from docker.models.containers import Container as DockerContainer

from .users import User


class _AttachStream(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


def matching_environment(
    patterns: list[str],
    *,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    excluded = exclude or set()
    return {
        key: value
        for key, value in os.environ.items()
        if key not in excluded and any(fnmatch.fnmatch(key, pattern) for pattern in patterns)
    }


def copy_directory(source: str, container: DockerContainer, target: str) -> None:
    with SpooledTemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for child in Path(source).iterdir():
                tar.add(child, arcname=child.name)
        archive.seek(0)
        container.put_archive(target, archive)


def start_attached(container: DockerContainer, *, interactive: bool) -> None:
    stream = cast(_AttachStream, container.attach_socket(params={
        "stdin": interactive,
        "stdout": True,
        "stderr": True,
        "stream": True,
        "logs": True,
    }))
    try:
        container.start()
        if interactive:
            def forward_input() -> None:
                try:
                    while chunk := os.read(sys.stdin.fileno(), 8192):
                        stream.write(chunk)
                except (AttributeError, OSError, ValueError):
                    pass

            threading.Thread(target=forward_input, daemon=True).start()

        while chunk := stream.read(8192):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    finally:
        stream.close()


@dataclass(frozen=True)
class ManagedContainer:
    id: str
    port: int
    token: str


class ContainerManager(Protocol):
    def start(self, user: User) -> ManagedContainer: ...
    def stop(self, container: ManagedContainer) -> None: ...
    def is_running(self, container: ManagedContainer) -> bool: ...
    def cleanup(self) -> None: ...
    def close(self) -> None: ...


class DockerManager:
    def __init__(
        self,
        *,
        image: str,
        port_range: range,
        instance: str,
        excluded_ports: set[int] | None = None,
        env_patterns: list[str] | None = None,
        client: DockerClient | None = None,
    ) -> None:
        self.image = image
        self.port_range = port_range
        self.instance = instance
        self.used_ports = set(excluded_ports or ())
        self.env_patterns = list(env_patterns or ())
        self.client = client or docker.from_env()

    @property
    def label(self) -> str:
        return f"xunx.instance={self.instance}"

    def _port_available(self, port: int) -> bool:
        if port in self.used_ports:
            return False
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def _allocate_port(self) -> int:
        available = [port for port in self.port_range if self._port_available(port)]
        if not available:
            raise RuntimeError("no available ports in the configured range")
        port = secrets.choice(available)
        self.used_ports.add(port)
        return port

    def start(self, user: User) -> ManagedContainer:
        port = self._allocate_port()
        container: DockerContainer | None = None
        try:
            container = self.client.containers.create(
                image=self.image,
                command=[
                    "xuns", "", "--host", "0.0.0.0", "--port", str(port),
                    "--token", user.token, "--base-path", user.base_path,
                ],
                name=f"xunx-{self.instance[:8]}-{user.name}",
                auto_remove=True,
                ports={f"{port}/tcp": ("127.0.0.1", port)},
                environment=matching_environment(self.env_patterns, exclude={"XUN_HOME"}),
                labels={"xunx.managed": "true", "xunx.instance": self.instance},
            )
            container.start()
            if not isinstance(container.id, str):
                raise RuntimeError("Docker SDK returned a container without an ID")
            return ManagedContainer(id=container.id, port=port, token=user.token)
        except BaseException:
            self.used_ports.discard(port)
            if container is not None:
                with suppress(NotFound):
                    container.remove(force=True)
            raise

    def stop(self, container: ManagedContainer) -> None:
        try:
            self.client.containers.get(container.id).remove(force=True)
        except NotFound:
            pass
        self.used_ports.discard(container.port)

    def is_running(self, container: ManagedContainer) -> bool:
        try:
            target = self.client.containers.get(container.id)
            target.reload()
            return target.status == "running"
        except NotFound:
            return False

    def cleanup(self) -> None:
        for container in self.client.containers.list(
            all=True,
            filters={"label": self.label},
            sparse=True,
            ignore_removed=True,
        ):
            with suppress(NotFound):
                container.remove(force=True)

    def close(self) -> None:
        self.client.close()


def instance_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]