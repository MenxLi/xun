
import os
import sys
import base64
import binascii
import fnmatch
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse
from datetime import datetime
from PIL import Image as PILImage, UnidentifiedImageError
from PIL.Image import Image
from .types import JsonType

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_IMAGE_FORMATS = {"GIF", "JPEG", "PNG", "WEBP"}

def image_to_url(image: str | Image) -> str:
    if isinstance(image, Image):
        buffered = BytesIO()
        image.save(buffered, format="PNG")
        image_bytes = buffered.getvalue()
        _validate_image_bytes(image_bytes)
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:image/png;base64,{encoded}"

    parsed = urlparse(image)
    if parsed.scheme in {"http", "https"}:
        return image
    if parsed.scheme == "data":
        try:
            header, encoded = image.split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise ValueError
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 image data") from exc
        _validate_image_bytes(image_bytes)
        return image

    image_path = Path(image).expanduser()
    if not image_path.exists() or not image_path.is_file():
        raise ValueError(f"Image file not found: {image}")

    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Failed to read image file {image}: {exc}") from exc

    _validate_image_bytes(image_bytes)
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"

def _validate_image_bytes(data: bytes) -> None:
    if not data:
        raise ValueError("Image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit")
    try:
        with PILImage.open(BytesIO(data)) as image:
            image.verify()
            if image.format not in SUPPORTED_IMAGE_FORMATS:
                raise ValueError("Supported image formats are PNG, JPEG, WebP, and GIF")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("File is not a supported image") from exc

def fmt_size(size: int | float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}PB"

def fmt_time(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

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

CONTAINER_ENV_PATTERNS = ["XUN_*", "_XUN_*"]

def parse_env_option(values: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Split --env values into forwarding patterns and NAME=VALUE assignments (like docker -e)."""
    patterns: list[str] = []
    assignments: dict[str, str] = {}
    for value in values:
        for item in (part.strip() for part in value.split(",")):
            if not item:
                continue
            name, sep, assigned = item.partition("=")
            if not sep:
                patterns.append(item)
            elif not (name := name.strip()):
                raise ValueError(f"environment assignment is missing a name: {item!r}")
            else:
                assignments[name] = assigned
    return patterns, assignments

def resolve_environment(
    patterns: Sequence[str],
    assignments: dict[str, str] | None = None,
    *,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """Forwarded variables with `assignments` on top; excluded names may not be assigned."""
    excluded = exclude or set()
    environment = matching_environment(list(patterns), exclude=excluded)
    for name, value in (assignments or {}).items():
        if name in excluded:
            print(f"[warn] {name} is not assignable, ignoring {name}={value!r}", file=sys.stderr)
            continue
        environment[name] = value
    return environment

def parse_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None

def to_json_object(
    obj: object, 
    try_methods: Sequence[str] = ("model_dump", "to_json", "value_json")
    ) -> JsonType:
    
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj

    for method in try_methods:
        if hasattr(obj, method):
            return getattr(obj, method)()
    
    if isinstance(obj, (list, tuple)):
        return [to_json_object(item, try_methods=try_methods) for item in obj]
    
    if isinstance(obj, dict):
        return {str(key): to_json_object(value, try_methods=try_methods) for key, value in obj.items()}

    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")