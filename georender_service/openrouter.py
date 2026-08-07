"""OpenRouter image-generation client.

georender uses this for two things, both of which are *data-driven*: the
`ai_image` symbolizer (repaints a control render of the real geometry) and
`openrouter://` asset files (icons/textures synthesized from a prompt that can
interpolate the real feature's properties).

Every generation is content-addressed on disk. That matters more here than for
`wms`: a call costs money and takes seconds, and a map render can trigger many.
Once a payload has been generated the cache makes the render reproducible and
offline — `generate_image()` checks the cache *before* it requires an API key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Image-output model on OpenRouter that accepts input images, which is what the
# control-image grounding needs. Overridable per-rule / per-asset.
DEFAULT_MODEL = "google/gemini-2.5-flash-image"
DEFAULT_TIMEOUT = 180.0
DEFAULT_REFERER = "https://github.com/openfantasymap/georender"
DEFAULT_TITLE = "georender"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"


class OpenRouterError(RuntimeError):
    """Raised when an image generation request cannot be satisfied."""


@dataclass(slots=True)
class OpenRouterConfig:
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    referer: str | None = DEFAULT_REFERER
    title: str | None = DEFAULT_TITLE
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        api_key_env: str | None = None,
        env: dict[str, str] | None = None,
    ) -> OpenRouterConfig:
        """Resolve config from `openrouter.json` + environment.

        Environment wins over the file for the secret and the base URL (so a
        container can inject the key without a mounted file); the file wins for
        the taste-level defaults (model, referer, title) since that's where a
        project pins them.
        """
        environ = os.environ if env is None else env
        file_data: dict[str, Any] = {}
        if config_path:
            path = Path(config_path)
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise OpenRouterError(f"{path} is not valid JSON: {exc}") from exc
                if isinstance(loaded, dict):
                    file_data = loaded

        key_env = api_key_env or str(file_data.get("api_key_env") or DEFAULT_API_KEY_ENV)
        api_key = environ.get(key_env) or file_data.get("api_key") or None
        base_url = environ.get("OPENROUTER_BASE_URL") or file_data.get("base_url") or DEFAULT_BASE_URL
        return cls(
            api_key=str(api_key) if api_key else None,
            base_url=str(base_url).rstrip("/"),
            model=str(file_data.get("model") or DEFAULT_MODEL),
            referer=str(file_data.get("referer") or DEFAULT_REFERER),
            title=str(file_data.get("title") or DEFAULT_TITLE),
            timeout=float(file_data.get("timeout") or DEFAULT_TIMEOUT),
        )


class OpenRouterClient:
    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        config: OpenRouterConfig | None = None,
        config_path: str | Path | None = None,
        api_key_env: str | None = None,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.config = config or OpenRouterConfig.load(config_path, api_key_env=api_key_env)
        # In-process memo so repeated rules in one render don't re-read the disk.
        self._memo: dict[str, bytes] = {}

    # -- caching ---------------------------------------------------------

    def cache_key(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[bytes] = (),
        extra: dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [hashlib.sha256(b).hexdigest() for b in images],
            "extra": extra or {},
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:32]

    def cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / "openrouter" / "gen" / f"{key}.png"

    def cached(self, key: str) -> bytes | None:
        if key in self._memo:
            return self._memo[key]
        path = self.cache_path(key)
        if path is not None and path.exists():
            data = path.read_bytes()
            self._memo[key] = data
            return data
        return None

    def store(self, key: str, data: bytes) -> None:
        self._memo[key] = data
        path = self.cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # -- generation ------------------------------------------------------

    def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        images: Sequence[bytes] = (),
        cache: bool = True,
        extra: dict[str, Any] | None = None,
        label: str = "",
    ) -> bytes:
        """Generate one image. Returns raw image bytes or raises OpenRouterError.

        `images` are input images (PNG bytes) sent alongside the prompt — this is
        how the control render grounds the output in the real geometry.
        """
        model_id = model or self.config.model
        key = self.cache_key(model=model_id, prompt=prompt, images=images, extra=extra)
        if cache:
            hit = self.cached(key)
            if hit is not None:
                return hit

        if not self.config.api_key:
            raise OpenRouterError(
                f"OpenRouter image generation requested{_suffix(label)} but no API key is "
                f"configured. Set ${DEFAULT_API_KEY_ENV} or add `api_key` to openrouter.json. "
                f"(cache key {key} was not on disk either)"
            )

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for blob in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(blob)},
                }
            )
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
        }
        for k, v in (extra or {}).items():
            body[str(k)] = v

        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        if self.config.referer:
            headers["HTTP-Referer"] = self.config.referer
        if self.config.title:
            headers["X-Title"] = self.config.title

        import httpx

        url = f"{self.config.base_url}/chat/completions"
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=self.config.timeout)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"network error calling OpenRouter{_suffix(label)}: {exc}") from exc

        if response.status_code >= 400:
            preview = response.content[:400].decode("utf-8", errors="replace").strip()
            raise OpenRouterError(
                f"OpenRouter returned HTTP {response.status_code}{_suffix(label)} for model "
                f"{model_id!r}: {preview}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            preview = response.content[:200].decode("utf-8", errors="replace").strip()
            raise OpenRouterError(f"OpenRouter response was not JSON{_suffix(label)}: {preview}") from exc

        data = _extract_image_bytes(payload)
        if data is None:
            raise OpenRouterError(
                f"OpenRouter returned no image{_suffix(label)} for model {model_id!r}: "
                f"{_describe_failure(payload)}"
            )
        if cache:
            self.store(key, data)
        else:
            self._memo[key] = data
        return data


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_image_bytes(payload: Any) -> bytes | None:
    """Pull the first image out of an OpenRouter chat-completion response.

    Image-output models are not uniform: the documented shape is
    `choices[].message.images[].image_url.url` holding a data URI, but some
    route content parts through `message.content[]` and some proxies mimic the
    OpenAI images endpoint (`data[].b64_json`). Try all three rather than
    hard-failing on a shape we didn't anticipate.
    """
    if not isinstance(payload, dict):
        return None

    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        for item in message.get("images") or []:
            data = _image_part_to_bytes(item)
            if data:
                return data
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                data = _image_part_to_bytes(item)
                if data:
                    return data

    # OpenAI-images-compatible shape.
    for item in payload.get("data") or []:
        data = _image_part_to_bytes(item)
        if data:
            return data
    return None


def _image_part_to_bytes(item: Any) -> bytes | None:
    if not isinstance(item, dict):
        return None
    image_url = item.get("image_url")
    if isinstance(image_url, dict):
        return _url_to_bytes(image_url.get("url"))
    if isinstance(image_url, str):
        return _url_to_bytes(image_url)
    for key in ("b64_json", "data", "image", "b64"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return _decode_maybe_data_uri(value)
    source = item.get("source")
    if isinstance(source, dict) and isinstance(source.get("data"), str):
        return _decode_maybe_data_uri(source["data"])
    url = item.get("url")
    if isinstance(url, str):
        return _url_to_bytes(url)
    return None


def _url_to_bytes(url: Any) -> bytes | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        return _decode_maybe_data_uri(url)
    if url.startswith("http://") or url.startswith("https://"):
        import httpx

        try:
            response = httpx.get(url, timeout=60.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"failed to download generated image from {url!r}: {exc}") from exc
        return response.content
    return None


def _decode_maybe_data_uri(value: str) -> bytes | None:
    payload = value
    if value.startswith("data:"):
        _, _, payload = value.partition(",")
        if not payload:
            return None
    try:
        return base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None


def _describe_failure(payload: dict[str, Any]) -> str:
    """Best-effort explanation for a response that carried no image.

    Refusals and quota errors both come back as ordinary 200s with text, so
    surfacing the text is the difference between a debuggable message and a
    mystery.
    """
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:400]
    if isinstance(error, str) and error:
        return error[:400]
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()[:400]
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    return item["text"].strip()[:400]
    return json.dumps(payload)[:400]


def _data_uri(blob: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _suffix(label: str) -> str:
    return f" [{label}]" if label else ""
