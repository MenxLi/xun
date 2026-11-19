"""Image generation and editing via a vllm-omni server (OpenAI-compatible).

Env: VLLM_OMNI_BASE_URL (required), VLLM_OMNI_API_KEY, VLLM_OMNI_MODEL.
APIs: https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/image_generation_api/
"""

import base64
import io
import os
from typing import Optional, Sequence

import httpx
import rich
from PIL import Image

from xun import ExtensionContext, ToolCallContext as Context, tool_attr

_BASE_URL = os.environ.get("VLLM_OMNI_BASE_URL", "").rstrip("/")
_API_KEY = os.environ.get("VLLM_OMNI_API_KEY", "") or "none"
_MODEL = os.environ.get("VLLM_OMNI_MODEL", "")
_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

if not _BASE_URL:
    rich.print("[Extension warning] im_gen disabled, set VLLM_OMNI_BASE_URL "
               "(optional: VLLM_OMNI_API_KEY, VLLM_OMNI_MODEL)")


def _check_detail(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("error", {}).get("message") or body.get("detail")
        except Exception:
            detail = resp.text[:500]
        raise RuntimeError(f"vllm-omni request failed ({resp.status_code}): {detail}")
    return resp


def generate_image(
    prompt: str,
    images: Sequence[Image.Image] = (),
    *,
    size: Optional[str] = None,
    n: int = 1,
    seed: Optional[int] = None,
    negative_prompt: Optional[str] = None,
    num_inference_steps: Optional[int] = None,
    guidance_scale: Optional[float] = None,
) -> list[Image.Image]:
    """Prompt (plus optional input images -> edit mode) to PIL images."""
    payload: dict = {"prompt": prompt, "n": n}
    if _MODEL:
        payload["model"] = _MODEL
    if seed is not None:
        payload["seed"] = seed
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if num_inference_steps is not None:
        payload["num_inference_steps"] = num_inference_steps
    if guidance_scale is not None:
        payload["guidance_scale"] = guidance_scale

    if images:
        endpoint = "/v1/images/edits"
        payload["size"] = size or "auto"
        files = []
        for i, img in enumerate(images):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            files.append(("image", (f"image_{i}.png", buf.getvalue(), "image/png")))
        form = {k: (str(v).lower() if isinstance(v, bool) else str(v))
                for k, v in payload.items()}
        resp = httpx.post(
            f"{_BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            data=form, files=files, timeout=_TIMEOUT,
        )
    else:
        endpoint = "/v1/images/generations"
        if size:
            payload["size"] = size
        resp = httpx.post(
            f"{_BASE_URL}{endpoint}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json=payload, timeout=_TIMEOUT,
        )

    _check_detail(resp)
    return [
        Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))).convert("RGB")
        for item in resp.json()["data"]
    ]


def _resolve_out(ctx: Context, path: str):
    """Resolve an output path in the agent workspace, gating overwrite like write_file."""
    from xun.tools.fs import confirm_write
    resolved = ctx.agent.workspace.resolve(path)
    if resolved.path.exists():
        confirm_write(ctx, resolved.path, resolved.in_tempdir,
                      f"File `{resolved.path}` already exists. Overwrite it with the generated image?")
    return resolved


def _show(ctx: Context, img: Image.Image, msg: str) -> None:
    """Feed the result image back for inspection; pointless without a vision model."""
    if "vision" in ctx.agent.config.model.capabilities:
        from xun.tools.common import defer_tool_image
        defer_tool_image(ctx, img, msg)


def _save(img: Image.Image, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}.get(
        path.suffix.lstrip(".").lower(), "PNG")
    if fmt == "JPEG" and img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, format=fmt)


@tool_attr(name="generate_image")
def tool_generate_image(
    ctx: Context,
    prompt: str,
    output_path: str,
    size: str = "1024x1024",
    seed: Optional[int] = None,
    negative_prompt: Optional[str] = None,
) -> str:
    """
    Generate an image from a text prompt, save it to output_path, and return the path.
    size: "WIDTHxHEIGHT", e.g. "1024x1024". seed: set for reproducibility.
    """
    img = generate_image(prompt, size=size, seed=seed,
                         negative_prompt=negative_prompt)[0]
    resolved = _resolve_out(ctx, output_path)
    _save(img, resolved.path)
    _show(ctx, img, f"Generated image saved to `{resolved.path}`:")
    return str(resolved.path)


@tool_attr(name="edit_image")
def tool_edit_image(
    ctx: Context,
    prompt: str,
    image_paths: list[str],
    output_path: str,
    size: str = "auto",
    seed: Optional[int] = None,
    negative_prompt: Optional[str] = None,
) -> str:
    """
    Edit existing image file(s) with a text prompt (e.g. style transfer, object
    add/remove/modify, combining several images), save to output_path, return the path.
    size: "auto" keeps the first input image's size, or give "WIDTHxHEIGHT".
    """
    images = [
        Image.open(ctx.agent.workspace.resolve(p).path).convert("RGB")
        for p in image_paths
    ]
    img = generate_image(prompt, images, size=None if size == "auto" else size,
                        seed=seed, negative_prompt=negative_prompt)[0]
    resolved = _resolve_out(ctx, output_path)
    _save(img, resolved.path)
    _show(ctx, img, f"Edited image saved to `{resolved.path}`:")
    return str(resolved.path)


def setup_extension(ctx: ExtensionContext) -> None:
    if not _BASE_URL:
        raise RuntimeError("VLLM_OMNI_BASE_URL not set, im_gen extension cannot be set up.")
    ctx.agent.toolbox.register(tool_generate_image, tool_edit_image)


if __name__ == "__main__":
    # Manual smoke test against a running server.
    print(f"base={_BASE_URL} model={_MODEL or '<server default>'}")

    cat = generate_image("a cute orange cat wearing sunglasses, studio photo",
                         size="512x512", seed=42)[0]
    cat.save(".tmp/im_gen_cat.png")
    print("generated ->", ".tmp/im_gen_cat.png", cat.size)

    hat = generate_image(
        "add a red birthday hat on the cat's head, keep everything else unchanged",
        [cat], seed=42)[0]
    hat.save(".tmp/im_gen_cat_edited.png")
    print("edited ->", ".tmp/im_gen_cat_edited.png", hat.size)

