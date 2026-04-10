from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class FileCache:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def tile_path(
        self,
        map_slug: str,
        ruleset: str,
        source_revision: str,
        ruleset_revision: str,
        renderer_revision: str,
        z: int,
        x: int,
        y: int,
        tile_params: dict[str, Any] | None = None,
    ) -> Path:
        cache_key = self._hash(
            {
                "map": map_slug,
                "ruleset": ruleset,
                "source_revision": source_revision,
                "ruleset_revision": ruleset_revision,
                "renderer_revision": renderer_revision,
                "tile_params": tile_params or {},
            }
        )
        return self.base_dir / "tiles" / map_slug / ruleset / cache_key / str(z) / str(x) / f"{y}.png"

    def image_path(
        self,
        map_slug: str,
        ruleset: str,
        source_revision: str,
        ruleset_revision: str,
        renderer_revision: str,
        image_params: dict[str, Any],
    ) -> Path:
        cache_key = self._hash(
            {
                "map": map_slug,
                "ruleset": ruleset,
                "source_revision": source_revision,
                "ruleset_revision": ruleset_revision,
                "renderer_revision": renderer_revision,
                "image": image_params,
            }
        )
        return self.base_dir / "images" / map_slug / ruleset / f"{cache_key}.png"

    def ad_hoc_image_path(
        self,
        ruleset: str,
        payload: dict[str, Any],
    ) -> Path:
        cache_key = self._hash(payload)
        return self.base_dir / "adhoc" / ruleset / f"{cache_key}.png"

    def read_bytes(self, path: Path) -> bytes | None:
        if not path.exists():
            return None
        return path.read_bytes()

    def write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
