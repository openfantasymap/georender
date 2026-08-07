from __future__ import annotations

import colorsys
import hashlib
import io
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter
from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import (
    WEB_MERCATOR_HALF,
    Viewport,
    ensure_mercator,
    geom_to_pixel,
    load_geom,
    mercator_bounds_for_features,
    mercator_tile_bounds,
    safe_tangent_angle,
    viewport_from_bounds,
)
from .openrouter import OpenRouterClient, OpenRouterError
from .rules import RulesetStore, feature_matches


@dataclass(slots=True)
class RenderContext:
    """Per-request facts a symbolizer may need beyond the viewport.

    Only `ai_image` reads this today: it needs the tile address to snap to an
    anchor cell, a way to fetch the features covering that (larger) cell, and
    the source revision so the anchor cache invalidates when the data changes.
    Everything is optional — renders without a context behave exactly as before.
    """

    tile: tuple[int, int, int] | None = None
    source_revision: str = ""
    source_crs: str = "EPSG:4326"
    # bounds_3857 -> list of GeoJSON features. Supplied by app.py so the renderer
    # can widen its own fetch without knowing anything about SourceStore.
    fetch_features: Callable[[tuple[float, float, float, float]], list[dict[str, Any]]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AssetStore:
    REMOTE_PREFIXES = ("http://", "https://", "github://", "openrouter://")
    GENERATOR_PREFIX = "openrouter://"

    def __init__(
        self,
        base_dir: str | Path,
        cache_dir: str | Path | None = None,
        openrouter: OpenRouterClient | None = None,
    ):
        self.base_dir = Path(base_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.openrouter = openrouter
        self.registry_path = self.base_dir / "assets.json"
        self.registry = self._load_registry()
        # Overlay registry of collections added at runtime (e.g. from a georender.json
        # bundle). Looked up before the on-disk registry, so a bundle can shadow or
        # extend the project's default collections for one render run.
        self._overlay_collections: dict[str, dict[str, Any]] = {}
        self._file_cache: dict[tuple[str, int], Image.Image] = {}
        self._materialized_cache: dict[
            tuple[str, int, int, float, bool, bool, float, float, str], Image.Image
        ] = {}

    def register_overlay(self, collections: dict[str, Any]) -> None:
        """Merge ad-hoc collection definitions into the runtime overlay registry.

        Used by `georender.json` bundle loading: a remote bundle's `assets` block
        lands here so its collections take precedence over `assets/assets.json`
        for the lifetime of the AssetStore.
        """
        for name, defn in (collections or {}).items():
            self._overlay_collections[str(name)] = defn

    def clear_overlay(self) -> None:
        self._overlay_collections.clear()

    def _all_collections(self) -> dict[str, Any]:
        merged = dict(self.registry.get("collections", {}))
        merged.update(self._overlay_collections)
        return merged

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"collections": {}}
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        data.setdefault("collections", {})
        return data

    def load(self, name: str, size_px: int | None = None) -> Image.Image:
        return self.load_for_ruleset(name, asset_collections=None, size_px=size_px)

    def load_for_ruleset(
        self,
        name: str,
        asset_collections: list[str] | dict[str, str] | None,
        size_px: int | None = None,
        seed: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> Image.Image:
        resolved_id, asset_def = self.resolve(name, asset_collections)
        return self._materialize_asset(
            resolved_id, asset_def, size_px=size_px, seed=seed, properties=properties
        )

    def resolve(
        self,
        name: str,
        asset_collections: list[str] | dict[str, str] | None,
    ) -> tuple[str, dict[str, Any]]:
        if not name:
            raise ValueError("Asset name is required")

        # Direct URI references skip the collection lookup entirely.
        if name.startswith(self.REMOTE_PREFIXES):
            return f"url:{name}", {"file": name}

        collections = self._all_collections()
        aliases = self._normalize_asset_collections(asset_collections)

        path = self.base_dir / name
        if path.exists():
            return f"file:{name}", {"file": name}

        if "." in name:
            prefix, asset_name = name.split(".", 1)
            collection_name = aliases.get(prefix, prefix)
            collection = collections.get(collection_name)
            if collection and asset_name in collection:
                return f"{collection_name}.{asset_name}", collection[asset_name]
            # Fallback: the ruleset aliased the prefix to a collection that
            # doesn't carry the asset, but a collection literally named `prefix`
            # does. This happens when a georender.json bundle declares a
            # collection under the same short name the ruleset uses as an alias
            # — e.g. ruleset says "vt": "valle_trebba" while the bundle ships
            # a collection called "vt" directly. Honour the literal prefix.
            if collection_name != prefix:
                direct = collections.get(prefix)
                if direct and asset_name in direct:
                    return f"{prefix}.{asset_name}", direct[asset_name]

        found: list[tuple[str, dict[str, Any]]] = []
        search_order = list(dict.fromkeys(list(aliases.values()) + list(collections.keys())))
        for collection_name in search_order:
            collection = collections.get(collection_name, {})
            if name in collection:
                found.append((f"{collection_name}.{name}", collection[name]))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ValueError(
                f"Asset '{name}' is ambiguous across collections; use a qualified reference like alias.{name}"
            )
        raise FileNotFoundError(f"Asset not found: {name}")

    def _normalize_asset_collections(self, asset_collections: list[str] | dict[str, str] | None) -> dict[str, str]:
        if asset_collections is None:
            return {}
        if isinstance(asset_collections, list):
            return {value: value for value in asset_collections}
        return {str(k): str(v) for k, v in asset_collections.items()}

    def _materialize_asset(
        self,
        resolved_id: str,
        asset_def: dict[str, Any],
        *,
        size_px: int | None,
        seed: str | None,
        properties: dict[str, Any] | None = None,
    ) -> Image.Image:
        seed_value = _stable_hash(seed or resolved_id)
        variant_index, variant = self._pick_variant(asset_def, seed_value)
        file_name = variant.get("file") or asset_def.get("file")
        if not file_name:
            raise ValueError(f"Asset '{resolved_id}' has no file or variants")

        # `openrouter://<model>` files are synthesized on demand. The prompt is
        # resolved here (not in _load_file) because it may interpolate the real
        # feature's properties, and because the resolved prompt — not the URI —
        # is what identifies the image in every cache below.
        generation: dict[str, Any] | None = None
        if str(file_name).startswith(self.GENERATOR_PREFIX):
            generation = self._generation_spec(
                resolved_id,
                asset_def,
                variant,
                file_name=str(file_name),
                seed=seed or resolved_id,
                properties=properties,
            )
            # Distinct prompts must not collide in _file_cache / _materialized_cache.
            file_name = f"{file_name}#{generation['prompt_hash']}"

        randomization = {}
        randomization.update(asset_def.get("randomization") or {})
        randomization.update(variant.get("randomization") or {})

        rotation = _choose_rotation(randomization.get("rotation"), seed_value)
        flip_x = bool(randomization.get("flip_x")) and bool((seed_value >> 3) & 1)
        flip_y = bool(randomization.get("flip_y")) and bool((seed_value >> 4) & 1)
        brightness = _choose_jitter_factor(randomization.get("brightness_jitter"), seed_value, shift=5)
        contrast = _choose_jitter_factor(randomization.get("contrast_jitter"), seed_value, shift=13)

        materialized_key = (
            resolved_id,
            int(size_px or 0),
            variant_index,
            float(rotation),
            flip_x,
            flip_y,
            round(brightness, 4),
            round(contrast, 4),
            # Two features can share one asset id but resolve to different
            # generated images (the prompt interpolates their properties), so
            # the prompt identity has to be part of the key.
            generation["prompt_hash"] if generation else "",
        )
        if materialized_key in self._materialized_cache:
            return self._materialized_cache[materialized_key].copy()

        img = self._load_file(file_name, size_px=size_px, generation=generation)
        if rotation:
            img = img.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        if flip_x:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_y:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if abs(brightness - 1.0) > 1e-6:
            img = ImageEnhance.Brightness(img).enhance(brightness)
        if abs(contrast - 1.0) > 1e-6:
            img = ImageEnhance.Contrast(img).enhance(contrast)

        self._materialized_cache[materialized_key] = img.copy()
        return img

    def _pick_variant(self, asset_def: dict[str, Any], seed_value: int) -> tuple[int, dict[str, Any]]:
        variants = asset_def.get("variants") or []
        if not variants:
            return 0, asset_def
        total = 0.0
        weights: list[float] = []
        for variant in variants:
            weight = float(variant.get("weight", 1))
            total += max(weight, 0.0)
            weights.append(max(weight, 0.0))
        if total <= 0:
            return 0, variants[0]
        needle = (seed_value % 10_000_000) / 10_000_000 * total
        acc = 0.0
        for idx, (variant, weight) in enumerate(zip(variants, weights, strict=False)):
            acc += weight
            if needle <= acc:
                return idx, variant
        return len(variants) - 1, variants[-1]

    def _load_file(
        self,
        name: str,
        size_px: int | None = None,
        *,
        generation: dict[str, Any] | None = None,
    ) -> Image.Image:
        key = (name, int(size_px or 0))
        if key in self._file_cache:
            return self._file_cache[key].copy()
        if generation is not None:
            path = self._generate_asset_file(generation)
        else:
            path = self._resolve_file_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Asset file not found: {name}")
        img = Image.open(path).convert("RGBA")
        if size_px:
            scale = size_px / max(img.width, img.height)
            w = max(1, int(round(img.width * scale)))
            h = max(1, int(round(img.height * scale)))
            img = img.resize((w, h), Image.LANCZOS)
        self._file_cache[key] = img.copy()
        return img

    def _resolve_file_path(self, name: str) -> Path:
        """Resolve an asset `file` reference to a local readable Path.

        Accepts:
          - bare names / relative paths → `base_dir / name` (existing behaviour).
          - `http://` and `https://` URLs → download to `cache_dir/assets/...`.
          - `github://<owner>/<repo>[@<ref>]/<path>` → expand to jsDelivr, then download.
        """
        if name.startswith("github://"):
            from .uris import expand_github_uri
            url = expand_github_uri(name)
            return self._fetch_remote_asset(url, source_ref=name)
        if name.startswith("http://") or name.startswith("https://"):
            return self._fetch_remote_asset(name, source_ref=name)
        return self.base_dir / name

    def _fetch_remote_asset(self, url: str, *, source_ref: str) -> Path:
        if not self.cache_dir:
            raise FileNotFoundError(
                f"Remote asset requested ({source_ref}) but AssetStore has no cache_dir; "
                "configure one in GeoRenderer to enable remote asset downloads."
            )
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        cache_path = self.cache_dir / "assets" / f"{digest}{suffix}"
        if cache_path.exists():
            return cache_path
        import httpx

        try:
            response = httpx.get(url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FileNotFoundError(f"Failed to fetch remote asset {source_ref}: {exc}") from exc
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        return cache_path

    # -- generated assets ------------------------------------------------

    def _generation_spec(
        self,
        resolved_id: str,
        asset_def: dict[str, Any],
        variant: dict[str, Any],
        *,
        file_name: str,
        seed: str,
        properties: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve an `openrouter://` asset into everything needed to generate it.

        Prompt fields are merged variant-over-asset, so a variant_set can share
        one base prompt and have each variant refine it.
        """
        merged: dict[str, Any] = {}
        merged.update(asset_def)
        merged.update(variant)

        model = file_name[len(self.GENERATOR_PREFIX):].strip() or None
        template = merged.get("prompt")
        if not template:
            raise ValueError(
                f"Asset '{resolved_id}' uses {self.GENERATOR_PREFIX} but has no `prompt`"
            )
        prompt = _format_prompt(str(template), properties)
        prompt = _decorate_asset_prompt(prompt, merged)

        # By default one prompt yields one image, shared by every feature that
        # uses the asset — that's what you want for a texture, and it keeps the
        # bill at one call. `vary_by_seed` opts into per-stamp variety.
        seed_part = seed if merged.get("vary_by_seed") else ""
        if seed_part:
            prompt = f"{prompt}\n\nVariation token (vary the composition, keep the subject): {seed_part}"

        return {
            "model": model,
            "prompt": prompt,
            "cache": bool(merged.get("cache", True)),
            "extra": merged.get("extra_body") or None,
            "api_key_env": merged.get("api_key_env"),
            "label": f"asset {resolved_id}",
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        }

    def _generate_asset_file(self, generation: dict[str, Any]) -> Path:
        client = self._openrouter_client(generation.get("api_key_env"))
        try:
            data = client.generate_image(
                generation["prompt"],
                model=generation.get("model"),
                cache=bool(generation.get("cache", True)),
                extra=generation.get("extra"),
                label=generation.get("label", ""),
            )
        except OpenRouterError as exc:
            raise FileNotFoundError(f"Failed to generate asset: {exc}") from exc

        # Must mirror generate_image's key exactly — including `extra` — or a
        # cached generation would be missed and re-materialized every render.
        key = client.cache_key(
            model=generation.get("model") or client.config.model,
            prompt=generation["prompt"],
            extra=generation.get("extra"),
        )
        path = client.cache_path(key)
        if path is not None and path.exists():
            return path
        # Nothing on disk (no cache_dir, or `cache: false`), but Image.open still
        # needs a file — write one out of the way of the user's asset directory.
        base = self.cache_dir / "openrouter" / "adhoc" if self.cache_dir else self.base_dir / ".generated"
        fallback = base / f"{generation['prompt_hash']}.png"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(data)
        return fallback

    def _openrouter_client(self, api_key_env: str | None = None) -> OpenRouterClient:
        if self.openrouter is None:
            self.openrouter = OpenRouterClient(cache_dir=self.cache_dir, api_key_env=api_key_env)
        return self.openrouter


class GeoRenderer:
    def __init__(
        self,
        rules_dir: str | Path,
        assets_dir: str | Path,
        cache_dir: str | Path | None = None,
        openrouter_config_path: str | Path | None = None,
    ):
        self.rules = RulesetStore(rules_dir, cache_dir=cache_dir)
        self.openrouter = OpenRouterClient(cache_dir=cache_dir, config_path=openrouter_config_path)
        self.assets = AssetStore(assets_dir, cache_dir=cache_dir, openrouter=self.openrouter)

    def render_png(
        self,
        geojson: dict[str, Any],
        ruleset_name: str,
        width: int,
        height: int,
        source_crs: str = "EPSG:4326",
        bbox: list[float] | None = None,
        padding_px: int = 32,
        bbox_crs: str = "EPSG:3857",
        context: RenderContext | None = None,
    ) -> bytes:
        ruleset = self.rules.load(ruleset_name)
        features = list(geojson.get("features", []))
        mercator_geoms = [ensure_mercator(load_geom(f), source_crs) for f in features]

        if bbox and len(bbox) == 4:
            from .geometry import ensure_mercator as _ensure
            from shapely.geometry import box
            bounds = _ensure(box(*bbox), bbox_crs).bounds
        else:
            bounds = mercator_bounds_for_features(mercator_geoms)
        viewport = viewport_from_bounds(bounds, width=width, height=height, padding_px=padding_px)
        image = self._render_scene(features, mercator_geoms, ruleset, viewport, context)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def render_tile_image(
        self,
        features: list[dict[str, Any]],
        mercator_geoms: list[BaseGeometry],
        ruleset_name: str,
        viewport: Viewport,
        context: RenderContext | None = None,
    ) -> Image.Image:
        ruleset = self.rules.load(ruleset_name)
        return self._render_scene(features, mercator_geoms, ruleset, viewport, context)

    def _render_scene(
        self,
        features: list[dict[str, Any]],
        mercator_geoms: list[BaseGeometry],
        ruleset: dict[str, Any],
        viewport: Viewport,
        context: RenderContext | None = None,
    ) -> Image.Image:
        bg = ruleset.get("background", "#00000000")
        image = Image.new("RGBA", (viewport.width, viewport.height), _parse_color(bg))

        indexed = list(zip(features, mercator_geoms, strict=False))
        rules = sorted(ruleset.get("rules", []), key=lambda r: r.get("z_index", r.get("z", 0)))
        asset_collections = ruleset.get("asset_collections")
        for rule in rules:
            self._apply_rule(image, indexed, viewport, rule, asset_collections, context)
        return image

    def _apply_rule(
        self,
        image: Image.Image,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        asset_collections: list[str] | dict[str, str] | None,
        context: RenderContext | None = None,
    ) -> None:
        symbolizer = rule["symbolizer"]
        stype = symbolizer["type"]
        rule_name = rule.get("name") or rule.get("id") or stype
        # Viewport-wide symbolizers don't iterate over features: they paint the
        # whole canvas once based on the viewport's bounds.
        if stype == "wms":
            self._render_wms(image, viewport, symbolizer, rule.get("edge_fade"), rule_name)
            return
        if stype == "ai_image":
            self._render_ai_image(image, indexed, viewport, rule, symbolizer, rule_name, context)
            return
        for feature_idx, (feature, world_geom) in enumerate(indexed):
            if world_geom.is_empty:
                continue
            geom_type = feature.get("geometry", {}).get("type")
            if not feature_matches(rule, feature, geom_type):
                continue
            pixel_geom = geom_to_pixel(world_geom, viewport)
            props = feature.get("properties") or {}
            if stype == "icon":
                self._render_icon(image, pixel_geom, symbolizer, asset_collections, rule_name, feature_idx, props)
            elif stype == "polygon_fill":
                self._render_polygon_fill(image, pixel_geom, symbolizer, rule.get("edge_fade"))
            elif stype == "polygon_pattern":
                self._render_polygon_pattern(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx, props)
            elif stype == "polygon_texture":
                self._render_polygon_texture(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx, props)
            elif stype == "line_pattern":
                self._render_line_pattern(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx, props)

    def _render_icon(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
        props: dict[str, Any] | None = None,
    ) -> None:
        opacity = float(symbolizer.get("opacity", 1.0))

        points: list[Point] = []
        if isinstance(geom, Point):
            points = [geom]
        elif isinstance(geom, MultiPoint):
            points = list(geom.geoms)
        elif isinstance(geom, (Polygon, MultiPolygon)):
            points = [geom.representative_point()]

        for pt in points:
            seed = f"icon|{rule_name}|{feature_idx}|{pt.x:.2f}|{pt.y:.2f}"
            asset = self.assets.load_for_ruleset(
                symbolizer["asset"],
                asset_collections=asset_collections,
                size_px=symbolizer.get("size_px", 24),
                seed=seed,
                properties=props,
            )
            asset = _apply_opacity(asset, opacity)
            x = int(round(pt.x - asset.width / 2))
            y = int(round(pt.y - asset.height / 2))
            image.alpha_composite(asset, dest=(x, y))

    def _render_polygon_fill(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        color = _parse_color(symbolizer.get("fill", "#ffffff80"))
        for poly in _iter_polygons(geom):
            _draw_polygon(draw, poly, fill=color)
        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_polygon_pattern(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
        props: dict[str, Any] | None = None,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        spacing = int(symbolizer.get("spacing_px", 32))
        tile_offset_x = int(symbolizer.get("offset_x_px", 0))
        tile_offset_y = int(symbolizer.get("offset_y_px", 0))
        opacity = float(symbolizer.get("opacity", 1.0))

        if symbolizer.get("asset"):
            size_px = symbolizer.get("size_px", 24)
            probe = self.assets.load_for_ruleset(
                symbolizer["asset"],
                asset_collections=asset_collections,
                size_px=size_px,
                seed=f"probe|{rule_name}|{feature_idx}",
                properties=props,
            )
            start_x = -probe.width
            end_x = image.size[0] + probe.width
            start_y = -probe.height
            end_y = image.size[1] + probe.height
            for x in range(start_x, end_x, spacing):
                for y in range(start_y, end_y, spacing):
                    seed = f"pattern|{rule_name}|{feature_idx}|{x}|{y}"
                    asset = self.assets.load_for_ruleset(
                        symbolizer["asset"],
                        asset_collections=asset_collections,
                        size_px=size_px,
                        seed=seed,
                        properties=props,
                    )
                    asset = _apply_opacity(asset, opacity)
                    layer.alpha_composite(asset, dest=(x + tile_offset_x, y + tile_offset_y))
        else:
            draw = ImageDraw.Draw(layer, "RGBA")
            fill = _parse_color(symbolizer.get("fill", "#ffffff55"))
            for x in range(0, image.size[0], spacing):
                for y in range(0, image.size[1], spacing):
                    r = max(1, int(symbolizer.get("dot_radius_px", 2)))
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_polygon_texture(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
        props: dict[str, Any] | None = None,
    ) -> None:
        """Tile a photorealistic texture across a polygon.

        Unlike `polygon_pattern`, the variant + rotation + brightness/contrast jitter
        from the asset definition are resolved ONCE per feature (using a feature-stable
        seed) and the resulting tile is repeated across the polygon bbox aligned to a
        global pixel grid. That keeps adjacent stamps visually continuous instead of
        producing a per-tile mosaic.

        Symbolizer fields:
          - asset (required): tileable texture asset reference.
          - tile_size_px (default 128): nominal tile edge.
          - rotation: feature-level rotation. Same shape as asset `randomization.rotation`
            (0|90|180|270|true|[angles...]). Default 0.
          - tint: optional CSS/hex color (with alpha) multiplied over the texture.
            `#7a8c4a80` muddies toward a 50% green; `#ffffff00` is a no-op.
          - opacity (default 1.0).
        """
        if geom.is_empty:
            return
        if not symbolizer.get("asset"):
            return

        feature_seed = f"texture|{rule_name}|{feature_idx}"
        tile_size_px = int(symbolizer.get("tile_size_px", symbolizer.get("size_px", 128)))
        opacity = float(symbolizer.get("opacity", 1.0))
        rotation_spec = symbolizer.get("rotation", 0)
        tint = symbolizer.get("tint")

        seed_value = _stable_hash(feature_seed)
        rotation_deg = _resolve_texture_rotation(rotation_spec, seed_value)

        tile = self.assets.load_for_ruleset(
            symbolizer["asset"],
            asset_collections=asset_collections,
            size_px=tile_size_px,
            seed=feature_seed,
            properties=props,
        )
        if rotation_deg:
            tile = tile.rotate(-rotation_deg, expand=True, resample=Image.BICUBIC)

        tw, th = tile.size
        if tw <= 0 or th <= 0:
            return

        poly_bounds = geom.bounds
        if not poly_bounds or len(poly_bounds) != 4:
            return
        minx, miny, maxx, maxy = poly_bounds

        layer_minx = max(0, int(minx) - tw)
        layer_miny = max(0, int(miny) - th)
        layer_maxx = min(image.size[0], int(maxx) + tw)
        layer_maxy = min(image.size[1], int(maxy) + th)
        if layer_maxx <= layer_minx or layer_maxy <= layer_miny:
            return

        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        # Align to a global grid so neighbouring polygons that share the same texture
        # + rotation tile through each other without re-phasing at the polygon edge.
        start_x = (layer_minx // tw) * tw
        start_y = (layer_miny // th) * th
        for y in range(start_y, layer_maxy + 1, th):
            for x in range(start_x, layer_maxx + 1, tw):
                layer.alpha_composite(tile, dest=(x, y))

        if tint:
            layer = _apply_tint(layer, _parse_color(tint))
        if opacity < 1.0:
            layer = _apply_opacity(layer, opacity)

        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_wms(
        self,
        image: Image.Image,
        viewport: Viewport,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        rule_name: str,
    ) -> None:
        """Composite a WMS GetMap response onto the canvas.

        Fields on the symbolizer:
          - url (required): base WMS endpoint.
          - layers (required): comma-separated layer names.
          - version: WMS protocol version, default "1.3.0" ("1.1.1" also OK).
          - format: image MIME, default "image/png" ("image/jpeg" for opaque
            aerial photos is usually smaller + faster).
          - crs: requested SRS/CRS, default "EPSG:3857" (the renderer's native
            projection — avoids reprojection cost AND the 1.3.0 geographic-CRS
            axis-order footgun).
          - styles: default "" (server default).
          - transparent: default true (false for opaque photos).
          - opacity: default 1.0, applied to the final RGBA layer.
          - extra_params: free-form dict of additional query-string params
            (e.g. ArcGIS-specific overrides).
          - cache: default true. Set false for layers whose upstream content
            changes (the 1976 aerofoto is fine to cache forever; weather isn't).
        """
        url = symbolizer.get("url")
        layers = symbolizer.get("layers")
        if not url or not layers:
            return  # silently skip — keeps a partially-configured ruleset renderable

        version = str(symbolizer.get("version", "1.3.0"))
        crs = str(symbolizer.get("crs", "EPSG:3857")).upper()
        bbox_str = _wms_bbox_for_viewport(viewport, crs, version)
        request_url = _wms_get_map_url(
            url,
            version=version,
            layers=str(layers),
            styles=str(symbolizer.get("styles", "")),
            crs=crs,
            bbox=bbox_str,
            width=viewport.width,
            height=viewport.height,
            fmt=str(symbolizer.get("format", "image/png")),
            transparent=bool(symbolizer.get("transparent", True)),
            extra=symbolizer.get("extra_params") or {},
        )

        png_bytes = self._fetch_wms(
            request_url,
            use_cache=bool(symbolizer.get("cache", True)),
            rule_name=rule_name,
        )
        if png_bytes is None:
            return

        try:
            layer_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        except Exception as exc:
            # Servers sometimes return an HTML/XML error masquerading as
            # image/png. Don't crash the whole render — drop this layer but
            # tell the operator so they can fix the URL.
            preview = png_bytes[:120].decode("utf-8", errors="replace").strip()
            sys.stderr.write(
                f"WARN [wms rule '{rule_name}']: response is not a decodable image ({exc}); "
                f"first bytes: {preview!r}\n"
            )
            return

        if layer_img.size != (viewport.width, viewport.height):
            layer_img = layer_img.resize((viewport.width, viewport.height), Image.LANCZOS)

        opacity = float(symbolizer.get("opacity", 1.0))
        if opacity < 1.0:
            layer_img = _apply_opacity(layer_img, opacity)

        if edge_fade and edge_fade.get("distance_px", 0) > 0:
            radius = float(edge_fade["distance_px"])
            mask = Image.new("L", layer_img.size, 255)
            mask = mask.filter(ImageFilter.GaussianBlur(radius=radius / 2))
            existing_alpha = layer_img.getchannel("A")
            layer_img.putalpha(ImageChopsMultiply(existing_alpha, mask))

        image.alpha_composite(layer_img)

    def _fetch_wms(self, url: str, *, use_cache: bool, rule_name: str) -> bytes | None:
        """Fetch a WMS response, optionally backed by the on-disk asset cache.

        Diagnostic logging is intentionally chatty: WMS errors are common
        (typos in the endpoint, server-side ServiceException, auth) and a
        silent drop hides them at render time.
        """
        cache_path: Path | None = None
        if use_cache and self.assets.cache_dir is not None:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            cache_path = self.assets.cache_dir / "wms" / f"{digest}.bin"
            if cache_path.exists():
                return cache_path.read_bytes()

        import httpx

        try:
            response = httpx.get(url, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            sys.stderr.write(
                f"WARN [wms rule '{rule_name}']: network error fetching {url!r}: {exc}\n"
            )
            return None
        if response.status_code >= 400:
            preview = response.content[:200].decode("utf-8", errors="replace").strip()
            sys.stderr.write(
                f"WARN [wms rule '{rule_name}']: HTTP {response.status_code} from {url!r}; "
                f"body: {preview!r}\n"
            )
            return None
        data = response.content
        # Don't cache HTML/XML/JSON error documents that the server may have
        # returned with a 200 status (some WMS servers ship ServiceException
        # XML with 200 OK). We only reject content-types that we KNOW are
        # error documents — `application/octet-stream` and friends are passed
        # through because some misconfigured WMS endpoints serve real image
        # bytes under a generic binary content-type; Image.open below will
        # reject anything that isn't actually an image.
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        error_prefixes = ("text/", "application/xml", "application/json", "application/vnd.ogc.")
        if content_type and any(content_type.startswith(p) for p in error_prefixes):
            preview = data[:200].decode("utf-8", errors="replace").strip()
            sys.stderr.write(
                f"WARN [wms rule '{rule_name}']: server returned non-image content-type "
                f"{content_type!r} for {url!r}; body: {preview!r}\n"
            )
            return None
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        return data

    # -- ai_image --------------------------------------------------------

    def _render_ai_image(
        self,
        image: Image.Image,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        symbolizer: dict[str, Any],
        rule_name: str,
        context: RenderContext | None,
    ) -> None:
        """Repaint a control render of the real geometry with an image model.

        The layer is always grounded: a flat colour-coded render of the features
        this rule matches (and/or the canvas painted so far) is sent as an input
        image, and the model is instructed to preserve every boundary. The
        descriptive legend / scene block is opt-in — see `_build_ai_prompt`.

        Symbolizer fields:
          - model: OpenRouter model id. Defaults to the client's configured model.
          - prompt: style/content instruction appended to the grounding preamble.
          - control: see `_build_control_image`.
          - describe_scene (default false): append extent, ground resolution and
            per-group feature counts, derived from the real data.
          - name_property / max_names: name-drop real features in that block.
          - anchor (default true on tile requests): generate once per coarse
            grid cell and crop, instead of once per tile.
          - anchor_zoom_delta (default 4) / anchor_zoom / anchor_size_px (1024).
          - max_side_px (default 1024): control image is downscaled to this before
            being sent; the result is scaled back up to the viewport.
          - opacity (default 1.0), edge_fade (rule level), cache (default true).
          - replace_canvas (default false): paste over everything beneath instead
            of alpha-compositing.
        """
        anchor_cfg = self._resolve_anchor(symbolizer, context)
        try:
            if anchor_cfg is None:
                layer_img = self._ai_image_for_viewport(
                    image, indexed, viewport, rule, symbolizer, rule_name, context
                )
            else:
                layer_img = self._ai_image_from_anchor(
                    image, indexed, viewport, rule, symbolizer, rule_name, context, anchor_cfg
                )
        except OpenRouterError as exc:
            # Same philosophy as `wms`: a generation failure drops the layer but
            # never kills the render, and it says why on stderr.
            sys.stderr.write(f"WARN [ai_image rule '{rule_name}']: {exc}\n")
            return
        if layer_img is None:
            return

        if layer_img.size != (viewport.width, viewport.height):
            layer_img = layer_img.resize((viewport.width, viewport.height), Image.LANCZOS)

        opacity = float(symbolizer.get("opacity", 1.0))
        if opacity < 1.0:
            layer_img = _apply_opacity(layer_img, opacity)

        edge_fade = rule.get("edge_fade")
        if edge_fade and edge_fade.get("distance_px", 0) > 0:
            radius = float(edge_fade["distance_px"])
            mask = Image.new("L", layer_img.size, 255)
            mask = mask.filter(ImageFilter.GaussianBlur(radius=radius / 2))
            layer_img.putalpha(ImageChopsMultiply(layer_img.getchannel("A"), mask))

        if symbolizer.get("replace_canvas"):
            image.paste(layer_img, (0, 0))
        else:
            image.alpha_composite(layer_img)

    def _resolve_anchor(
        self, symbolizer: dict[str, Any], context: RenderContext | None
    ) -> tuple[int, int, int, int] | None:
        """Return `(z, x, y, size_px)` of the anchor cell, or None for direct render.

        Anchoring only applies to tile requests: `image.png` already renders one
        viewport, so there is nothing to share.
        """
        if context is None or context.tile is None:
            return None
        if not symbolizer.get("anchor", True):
            return None
        z, x, y = context.tile
        if "anchor_zoom" in symbolizer:
            anchor_z = int(symbolizer["anchor_zoom"])
        else:
            anchor_z = z - int(symbolizer.get("anchor_zoom_delta", 4))
        anchor_z = max(0, min(anchor_z, z))
        shift = z - anchor_z
        size_px = int(symbolizer.get("anchor_size_px", 1024))
        return anchor_z, x >> shift, y >> shift, size_px

    def _ai_image_for_viewport(
        self,
        canvas: Image.Image,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        symbolizer: dict[str, Any],
        rule_name: str,
        context: RenderContext | None,
    ) -> Image.Image | None:
        control, legend = self._build_control_image(indexed, viewport, rule, symbolizer, canvas)
        prompt = self._build_ai_prompt(symbolizer, legend, viewport, rule_name)
        images = _control_payload(control, int(symbolizer.get("max_side_px", 1024)))
        data = self._openrouter().generate_image(
            prompt,
            model=symbolizer.get("model"),
            images=images,
            cache=bool(symbolizer.get("cache", True)),
            extra=symbolizer.get("extra_body") or None,
            # No label: the caller's warning already carries the rule name.
        )
        return _decode_generated_image(data, rule_name)

    def _ai_image_from_anchor(
        self,
        canvas: Image.Image,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        symbolizer: dict[str, Any],
        rule_name: str,
        context: RenderContext | None,
        anchor: tuple[int, int, int, int],
    ) -> Image.Image | None:
        anchor_z, anchor_x, anchor_y, size_px = anchor
        anchor_bounds = mercator_tile_bounds(anchor_x, anchor_y, anchor_z)
        anchor_viewport = Viewport(*anchor_bounds, width=size_px, height=size_px)

        anchor_img = self._load_anchor_cache(symbolizer, rule_name, anchor, context)
        if anchor_img is None:
            control_source = str((symbolizer.get("control") or {}).get("source", "features")).lower()
            if control_source in ("canvas", "both"):
                # The canvas we hold covers one tile, not the anchor cell, so it
                # cannot be used as a control here. Fall back to the flat render
                # rather than sending a misaligned image — and say so.
                sys.stderr.write(
                    f"WARN [ai_image rule '{rule_name}']: control.source={control_source!r} is "
                    f"not available under anchor-grid generation (the canvas covers one tile, "
                    f"the anchor covers many); falling back to the flat feature render. Set "
                    f"\"anchor\": false to use the canvas, at one generation per tile.\n"
                )
            # The features handed to us cover one small tile; the anchor cell is
            # 4^delta times larger, so re-fetch for its bounds or the control
            # render would be a sliver of the real data.
            anchor_indexed = self._features_for_bounds(anchor_bounds, indexed, context, rule_name)
            control, legend = self._build_control_image(
                anchor_indexed, anchor_viewport, rule, symbolizer, None
            )
            prompt = self._build_ai_prompt(symbolizer, legend, anchor_viewport, rule_name)
            images = _control_payload(control, int(symbolizer.get("max_side_px", 1024)))
            data = self._openrouter().generate_image(
                prompt,
                model=symbolizer.get("model"),
                images=images,
                cache=bool(symbolizer.get("cache", True)),
                extra=symbolizer.get("extra_body") or None,
                label=f"anchor {anchor_z}/{anchor_x}/{anchor_y}",
            )
            anchor_img = _decode_generated_image(data, rule_name)
            if anchor_img is None:
                return None
            if anchor_img.size != (size_px, size_px):
                anchor_img = anchor_img.resize((size_px, size_px), Image.LANCZOS)
            self._store_anchor_cache(symbolizer, rule_name, anchor, context, anchor_img)

        return _crop_viewport_from_anchor(anchor_img, anchor_viewport, viewport)

    def _features_for_bounds(
        self,
        bounds: tuple[float, float, float, float],
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        context: RenderContext | None,
        rule_name: str,
    ) -> list[tuple[dict[str, Any], BaseGeometry]]:
        if context is None or context.fetch_features is None:
            sys.stderr.write(
                f"WARN [ai_image rule '{rule_name}']: no feature fetcher in the render "
                f"context; the anchor cell will only see this tile's features\n"
            )
            return indexed
        features = context.fetch_features(bounds) or []
        geoms = [ensure_mercator(load_geom(f), context.source_crs) for f in features]
        return list(zip(features, geoms, strict=False))

    def _anchor_cache_path(
        self,
        symbolizer: dict[str, Any],
        rule_name: str,
        anchor: tuple[int, int, int, int],
        context: RenderContext | None,
    ) -> Path | None:
        """Cheap cache keyed on identity rather than content.

        The content-addressed cache in OpenRouterClient can only be consulted
        after building the control image, which needs a source fetch. Keying on
        (rule, symbolizer, anchor cell, source revision) lets a warm tile skip
        that entirely — which is the whole point of the anchor grid.
        """
        if not self.assets.cache_dir:
            return None
        anchor_z, anchor_x, anchor_y, size_px = anchor
        payload = {
            "rule": rule_name,
            "symbolizer": symbolizer,
            "anchor": [anchor_z, anchor_x, anchor_y, size_px],
            "source_revision": context.source_revision if context else "",
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()[:32]
        return self.assets.cache_dir / "openrouter" / "anchors" / f"{digest}.png"

    def _load_anchor_cache(
        self,
        symbolizer: dict[str, Any],
        rule_name: str,
        anchor: tuple[int, int, int, int],
        context: RenderContext | None,
    ) -> Image.Image | None:
        if not symbolizer.get("cache", True):
            return None
        path = self._anchor_cache_path(symbolizer, rule_name, anchor, context)
        if path is None or not path.exists():
            return None
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            return None

    def _store_anchor_cache(
        self,
        symbolizer: dict[str, Any],
        rule_name: str,
        anchor: tuple[int, int, int, int],
        context: RenderContext | None,
        img: Image.Image,
    ) -> None:
        if not symbolizer.get("cache", True):
            return
        path = self._anchor_cache_path(symbolizer, rule_name, anchor, context)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, format="PNG")

    def _openrouter(self) -> OpenRouterClient:
        if getattr(self, "openrouter", None) is None:
            self.openrouter = OpenRouterClient(cache_dir=self.assets.cache_dir)
            self.assets.openrouter = self.openrouter
        return self.openrouter

    # -- ai_image: grounding ---------------------------------------------

    def _build_control_image(
        self,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        symbolizer: dict[str, Any],
        canvas: Image.Image | None,
    ) -> tuple[list[Image.Image], list[dict[str, Any]]]:
        """Render the real geometry into flat colour regions the model can follow.

        Returns `(images_to_send, legend)`. The legend rows carry the resolved
        hex colour, the group key, a description if the ruleset supplied one,
        and the feature count — so the prompt text can never disagree with the
        pixels that were actually drawn.

        `control.source` picks what gets sent:
          - "features" (default): the flat colour render only.
          - "canvas": the scene painted by lower-z_index rules, as-is.
          - "both": canvas first (for palette/context), flat render second (for
            structure). Two input images, one call.

        `control.draw_order` is "area" (largest first) by default; "source" keeps
        the feature order as fetched.
        """
        control = symbolizer.get("control") or {}
        source = str(control.get("source", "features")).lower()
        group_by = control.get("group_by")
        group_defs = control.get("groups") or {}
        background = _parse_color(control.get("background", "#000000ff"))
        line_width = max(1, int(control.get("line_width_px", 6)))
        point_radius = max(1, int(control.get("point_radius_px", 8)))
        rule_name = rule.get("name") or rule.get("id") or "ai_image"

        layer = Image.new("RGBA", (viewport.width, viewport.height), background)
        draw = ImageDraw.Draw(layer, "RGBA")
        legend: dict[str, dict[str, Any]] = {}

        matched = [
            (feature, world_geom)
            for feature, world_geom in indexed
            if not world_geom.is_empty
            and _control_feature_matches(rule, feature, feature.get("geometry", {}).get("type"))
        ]
        # Draw biggest first so small features land on top. A single rule paints
        # every feature in one pass, so it has none of the per-rule z_index that
        # keeps the normal symbolizers layered — without this, one viewport-sized
        # backdrop polygon buries every unit inside it. Lines and points have zero
        # area and therefore always end up on top, which is what you want.
        if str(control.get("draw_order", "area")).lower() == "area":
            matched.sort(key=lambda pair: -pair[1].area)

        for feature, world_geom in matched:
            props = feature.get("properties") or {}
            key = str(props.get(group_by)) if group_by else rule_name
            entry = legend.get(key)
            if entry is None:
                defn = group_defs.get(key) or {}
                color_hex = str(defn.get("color") or _auto_control_color(key))
                entry = {
                    "key": key,
                    "color": color_hex,
                    "describe": defn.get("describe") or defn.get("description"),
                    "count": 0,
                    "names": [],
                }
                legend[key] = entry
            entry["count"] += 1
            name = props.get(str(control.get("name_property", symbolizer.get("name_property", "name"))))
            if name and len(entry["names"]) < 24:
                entry["names"].append(str(name))

            pixel_geom = geom_to_pixel(world_geom, viewport)
            _draw_control_geometry(
                draw,
                pixel_geom,
                _parse_color(entry["color"]),
                background,
                line_width=line_width,
                point_radius=point_radius,
            )

        legend_rows = list(legend.values())
        flat = layer.convert("RGB").convert("RGBA")

        images: list[Image.Image] = []
        if source == "canvas":
            images = [canvas.copy()] if canvas is not None else [flat]
        elif source == "both":
            if canvas is not None:
                images = [canvas.copy(), flat]
            else:
                images = [flat]
        else:
            images = [flat]
        return images, legend_rows

    def _build_ai_prompt(
        self,
        symbolizer: dict[str, Any],
        legend: list[dict[str, Any]],
        viewport: Viewport,
        rule_name: str,
    ) -> str:
        """Assemble the prompt: grounding preamble + optional style/legend/scene.

        The preamble is unconditional — it is what makes the control image
        authoritative. Everything after it is opt-in per rule, which is why a
        bare `{"type": "ai_image"}` still produces a data-faithful repaint.
        """
        control = symbolizer.get("control") or {}
        blocks: list[str] = [_AI_PREAMBLE]

        style = symbolizer.get("prompt")
        if style:
            blocks.append(f"Style and content direction:\n{str(style).strip()}")

        has_describe = any(row.get("describe") for row in legend)
        include_legend = bool(control.get("include_legend", has_describe))
        if include_legend and legend:
            lines = []
            for row in sorted(legend, key=lambda r: r["key"]):
                described = row.get("describe") or f"the map layer named {row['key']!r}"
                lines.append(f"  {row['color']} -> {described}")
            blocks.append(
                "Colour legend for the control image. Replace each flat colour region "
                "with the described material, painted in place and covering the colour "
                "completely:\n"
                + "\n".join(lines)
                + "\nEvery colour listed above must be absent from the output image. "
                "Any area not listed is background: render it as neutral terrain."
            )

        if symbolizer.get("describe_scene"):
            blocks.append(_describe_scene(legend, viewport, symbolizer))

        return "\n\n".join(b for b in blocks if b)

    def _render_line_pattern(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
        props: dict[str, Any] | None = None,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        spacing = float(symbolizer.get("spacing_px", 20))
        base_size_px = symbolizer.get("size_px", 16)
        buffer_px = float(symbolizer.get("buffer_px", base_size_px / 2))
        cross_step_px = float(symbolizer.get("cross_step_px", max(8, buffer_px)))
        rotate = bool(symbolizer.get("rotate", True))
        opacity = float(symbolizer.get("opacity", 1.0))
        probe = self.assets.load_for_ruleset(
            symbolizer["asset"],
            asset_collections=asset_collections,
            size_px=base_size_px,
            seed=f"probe|line|{rule_name}|{feature_idx}",
            properties=props,
        )

        offsets = [0.0]
        if buffer_px > probe.height * 0.75:
            max_offset = max(0.0, buffer_px - probe.height / 2)
            rails = int(max_offset // max(cross_step_px, 1))
            offsets = [0.0]
            for i in range(1, rails + 1):
                offsets.extend([i * cross_step_px, -i * cross_step_px])
            offsets = [o for o in offsets if abs(o) <= max_offset + 1e-6]

        line_mask_geom = geom.buffer(buffer_px, cap_style=2, join_style=2)
        for line_idx, line in enumerate(_iter_lines(geom)):
            for offset in offsets:
                target_line = _offset_line(line, offset)
                if target_line.is_empty:
                    continue
                length = target_line.length
                if length <= 0:
                    continue
                d = 0.0
                while d <= length:
                    pt = target_line.interpolate(d)
                    seed = f"line|{rule_name}|{feature_idx}|{line_idx}|{offset:.2f}|{d:.2f}"
                    stamp = self.assets.load_for_ruleset(
                        symbolizer["asset"],
                        asset_collections=asset_collections,
                        size_px=base_size_px,
                        seed=seed,
                        properties=props,
                    )
                    if rotate:
                        angle = safe_tangent_angle(target_line, d)
                        stamp = stamp.rotate(-angle, expand=True, resample=Image.BICUBIC)
                    stamp = _apply_opacity(stamp, opacity)
                    x = int(round(pt.x - stamp.width / 2))
                    y = int(round(pt.y - stamp.height / 2))
                    layer.alpha_composite(stamp, dest=(x, y))
                    d += spacing

        mask = self._mask_for_geometry(image.size, line_mask_geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _mask_for_geometry(
        self,
        size: tuple[int, int],
        geom: BaseGeometry,
        edge_fade: dict[str, Any] | None,
    ) -> Image.Image:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask, "L")
        for poly in _iter_polygons(geom):
            _draw_polygon(draw, poly, fill=255)
        if edge_fade and edge_fade.get("distance_px", 0) > 0:
            radius = float(edge_fade.get("distance_px", 0))
            mask = mask.filter(ImageFilter.GaussianBlur(radius=radius / 2))
        return mask


def ImageChopsMultiply(a: Image.Image, b: Image.Image) -> Image.Image:
    return ImageChops.multiply(a, b)


def _parse_color(value: str | tuple[int, int, int] | tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, tuple):
        if len(value) == 3:
            return (*value, 255)
        return value
    rgba = ImageColor.getcolor(value, "RGBA")
    return rgba


from .uris import expand_github_uri as _expand_github_uri  # re-exported for tests


def _wms_bbox_for_viewport(viewport: Viewport, crs: str, version: str) -> str:
    """Build the BBOX query string for a WMS GetMap matching the viewport.

    The viewport is always EPSG:3857. If the WMS server speaks a different CRS
    we reproject the corners. WMS 1.3.0 also flipped the axis order for
    geographic CRSes (EPSG:4326 / CRS:84 → lat,lon instead of lon,lat) — handle
    that here so the symbolizer config doesn't have to.
    """
    minx, miny, maxx, maxy = viewport.minx, viewport.miny, viewport.maxx, viewport.maxy
    crs_upper = crs.upper()
    if crs_upper not in ("EPSG:3857", "EPSG:900913"):
        try:
            from pyproj import Transformer

            transformer = Transformer.from_crs("EPSG:3857", crs_upper, always_xy=True)
            minx, miny = transformer.transform(viewport.minx, viewport.miny)
            maxx, maxy = transformer.transform(viewport.maxx, viewport.maxy)
        except Exception:
            # Fall back to passing the raw mercator numbers — the server will
            # reject the request loudly, which is the right failure mode.
            pass
    if version == "1.3.0" and crs_upper in ("EPSG:4326", "CRS:84"):
        return f"{miny},{minx},{maxy},{maxx}"
    return f"{minx},{miny},{maxx},{maxy}"


def _wms_get_map_url(
    base: str,
    *,
    version: str,
    layers: str,
    styles: str,
    crs: str,
    bbox: str,
    width: int,
    height: int,
    fmt: str,
    transparent: bool,
    extra: dict[str, Any],
) -> str:
    from urllib.parse import urlencode

    params: dict[str, str] = {
        "SERVICE": "WMS",
        "VERSION": version,
        "REQUEST": "GetMap",
        "LAYERS": layers,
        "STYLES": styles,
        # WMS 1.1.1 calls it SRS; 1.3.0 calls it CRS. Send both — extra ones
        # are harmless and some servers are picky.
        ("CRS" if version == "1.3.0" else "SRS"): crs,
        "BBOX": bbox,
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": fmt,
        "TRANSPARENT": "TRUE" if transparent else "FALSE",
    }
    for k, v in extra.items():
        params[str(k).upper()] = str(v)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(params)}"


# ---------------------------------------------------------------------------
# ai_image helpers
# ---------------------------------------------------------------------------

# Unconditional grounding instruction. This is the contract that makes the
# control image authoritative rather than a mood board — it ships on every
# request, even when the ruleset supplies no prompt of its own.
_AI_PREAMBLE = (
    "You are given a control image that encodes the exact geographic layout of one "
    "map viewport, rendered from real geospatial data. Each flat colour region marks "
    "a real feature at its real position.\n"
    "The colours are an encoding, not content. Produce a finished map image in which "
    "every flat colour region has been REPLACED by the real-world material it stands "
    "for. None of the flat control colours may remain visible anywhere in the output — "
    "a region still showing its control colour is a failed render. Blend each repainted "
    "region into its surroundings the way the real terrain would look.\n"
    "Regions may be small, thin, or repeated many times over. They are still real ground, "
    "not annotations. Render every one of them — including long narrow strips and dense "
    "clusters — as actual terrain material, textured and lit like the rest of the scene, "
    "never as a flat shape, a translucent wash, a tinted patch or an outline laid over it. "
    "Change what a region is made of, never where it is: its texture changes, its outline "
    "does not. Every strip stays exactly where the control image puts it, at the same "
    "length, width and angle, and none may be dropped, merged or relocated.\n"
    "Preserve the geometry exactly: do not move, rotate, reshape, add, remove, crop or "
    "re-frame anything. Every boundary stays where it is, so the output registers "
    "pixel-for-pixel over the control image, with the same aspect ratio and framing. "
    "North is up.\n"
    "Output imagery only. Draw no text of any kind — no labels, place names, callouts, "
    "legends, scale bars, north arrows, borders or watermarks."
)


def _control_feature_matches(rule: dict[str, Any], feature: dict[str, Any], geom_type: str | None) -> bool:
    """Match a feature for the control render.

    `ai_image` is a viewport symbolizer, so its rule may legitimately omit the
    `geometry` whitelist that `feature_matches` insists on. When it's omitted,
    accept any geometry and let `filter` do the selecting.
    """
    if not geom_type:
        return False
    if rule.get("geometry"):
        return feature_matches(rule, feature, geom_type)
    probe = dict(rule)
    probe["geometry"] = [geom_type]
    return feature_matches(probe, feature, geom_type)


def _auto_control_color(key: str) -> str:
    """Deterministic, well-separated control colour for an undeclared group.

    Derived from a hash of the group key rather than from its index in the
    observed set: two neighbouring anchor cells that happen to contain different
    subsets of layers must still paint the same layer the same colour, or their
    repaints won't agree.
    """
    seed = _stable_hash(key)
    hue = (seed % 360) / 360.0
    saturation = min(1.0, 0.85 + ((seed >> 9) & 7) / 56.0)
    value = min(1.0, 0.80 + ((seed >> 13) & 7) / 40.0)
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def _draw_control_geometry(
    draw: ImageDraw.ImageDraw,
    geom: BaseGeometry,
    color: tuple[int, int, int, int],
    background: tuple[int, int, int, int],
    *,
    line_width: int,
    point_radius: int,
) -> None:
    if geom.is_empty:
        return
    geom_type = geom.geom_type
    if geom_type in ("Polygon", "MultiPolygon"):
        for poly in _iter_polygons(geom):
            draw.polygon([(c[0], c[1]) for c in poly.exterior.coords], fill=color)
            # Holes go back to background, not to transparent — the model reads
            # this as "nothing here", which is what a ring hole means.
            for hole in poly.interiors:
                draw.polygon([(c[0], c[1]) for c in hole.coords], fill=background)
        return
    if geom_type in ("LineString", "MultiLineString"):
        for line in _iter_lines(geom):
            coords = [(c[0], c[1]) for c in line.coords]
            if len(coords) >= 2:
                draw.line(coords, fill=color, width=line_width, joint="curve")
        return
    if geom_type == "Point":
        _draw_control_point(draw, geom, color, point_radius)
        return
    if geom_type == "MultiPoint":
        for point in geom.geoms:
            _draw_control_point(draw, point, color, point_radius)
        return
    if geom_type == "GeometryCollection":
        for part in geom.geoms:
            _draw_control_geometry(
                draw, part, color, background, line_width=line_width, point_radius=point_radius
            )


def _draw_control_point(
    draw: ImageDraw.ImageDraw, point: Point, color: tuple[int, int, int, int], radius: int
) -> None:
    draw.ellipse((point.x - radius, point.y - radius, point.x + radius, point.y + radius), fill=color)


def _control_payload(images: list[Image.Image], max_side_px: int) -> list[bytes]:
    """Encode control images for transport, downscaled to `max_side_px`.

    Generation cost and latency scale with input size, and the models cap their
    output resolution anyway — sending a 4096px control buys nothing.
    """
    payload: list[bytes] = []
    for img in images:
        out = img
        longest = max(out.width, out.height)
        if max_side_px > 0 and longest > max_side_px:
            scale = max_side_px / longest
            out = out.resize(
                (max(1, int(round(out.width * scale))), max(1, int(round(out.height * scale)))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        payload.append(buf.getvalue())
    return payload


def _decode_generated_image(data: bytes, rule_name: str) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:
        preview = data[:120].decode("utf-8", errors="replace").strip()
        sys.stderr.write(
            f"WARN [ai_image rule '{rule_name}']: generated payload is not a decodable "
            f"image ({exc}); first bytes: {preview!r}\n"
        )
        return None


def _crop_viewport_from_anchor(
    anchor_img: Image.Image, anchor_viewport: Viewport, viewport: Viewport
) -> Image.Image | None:
    """Cut the requested viewport out of a generated anchor-cell image.

    A tile viewport carries a buffer, so at a cell's edge the requested window
    can overhang the anchor. PIL pads an out-of-range crop with transparent
    pixels rather than stretching, so the part that does exist stays
    geometrically exact and only the overhang is empty.
    """
    left = (viewport.minx - anchor_viewport.minx) / anchor_viewport.x_span * anchor_img.width
    right = (viewport.maxx - anchor_viewport.minx) / anchor_viewport.x_span * anchor_img.width
    top = (anchor_viewport.maxy - viewport.maxy) / anchor_viewport.y_span * anchor_img.height
    bottom = (anchor_viewport.maxy - viewport.miny) / anchor_viewport.y_span * anchor_img.height

    box = (
        int(math.floor(left)),
        int(math.floor(top)),
        int(math.ceil(right)),
        int(math.ceil(bottom)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    crop = anchor_img.crop(box)
    if crop.size != (viewport.width, viewport.height):
        crop = crop.resize((viewport.width, viewport.height), Image.LANCZOS)
    return crop


def _mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x / WEB_MERCATOR_HALF * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp(y / WEB_MERCATOR_HALF * math.pi)) - math.pi / 2.0)
    return lon, lat


def _describe_scene(
    legend: list[dict[str, Any]], viewport: Viewport, symbolizer: dict[str, Any]
) -> str:
    """Facts about the viewport, all derived from the data actually being rendered.

    Ground resolution corrects the Web Mercator x-span by cos(latitude) — the
    raw 3857 span would overstate real-world size by ~40% at these latitudes,
    and the model does use the number to pick a level of detail.
    """
    west, south = _mercator_to_lonlat(viewport.minx, viewport.miny)
    east, north = _mercator_to_lonlat(viewport.maxx, viewport.maxy)
    center_lat = (south + north) / 2.0
    ground_width_m = viewport.x_span * math.cos(math.radians(center_lat))
    resolution = ground_width_m / max(viewport.width, 1)

    lines = [
        "Scene facts, measured from the source data (use them to pick scale and detail):",
        f"  extent: {west:.6f},{south:.6f} to {east:.6f},{north:.6f} (EPSG:4326)",
        f"  ground resolution: {resolution:.2f} m per pixel; the image spans about "
        f"{ground_width_m / 1000.0:.2f} km east-west",
        "  orientation: north is up",
    ]
    if legend:
        counts = ", ".join(
            f"{row['count']} x {row['key']}" for row in sorted(legend, key=lambda r: -r["count"])
        )
        lines.append(f"  features present: {counts}")
        max_names = int(symbolizer.get("max_names", 12))
        names: list[str] = []
        for row in sorted(legend, key=lambda r: r["key"]):
            names.extend(row.get("names") or [])
        if names and max_names > 0:
            shown = names[:max_names]
            suffix = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
            lines.append(f"  named features: {', '.join(shown)}{suffix}")
    return "\n".join(lines)


_PROMPT_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _format_prompt(template: str, properties: dict[str, Any] | None) -> str:
    """Interpolate `{property}` placeholders from a feature's real properties.

    Deliberately not `str.format`: prompts contain prose, and a stray brace
    shouldn't blow up a render. Unknown placeholders collapse to empty.
    """
    props = properties or {}

    def _sub(match: re.Match[str]) -> str:
        value = props.get(match.group(1))
        return "" if value is None else str(value)

    return _PROMPT_PLACEHOLDER.sub(_sub, template).strip()


def _decorate_asset_prompt(prompt: str, spec: dict[str, Any]) -> str:
    """Append the mechanical requirements implied by how the asset gets used."""
    extras: list[str] = []
    if spec.get("tileable"):
        extras.append(
            "The image must tile seamlessly: opposite edges have to match exactly, "
            "with no visible seam, border, vignette or frame."
        )
    if spec.get("transparent"):
        extras.append(
            "Isolate the subject on a fully transparent background (PNG alpha). "
            "No backdrop, no ground shadow, no padding."
        )
    kind = str(spec.get("kind") or "")
    if kind == "icon":
        extras.append("Render it as a single centred map symbol, viewed straight on.")
    elif kind == "texture":
        extras.append("Render it as a flat top-down material swatch with even lighting.")
    if not extras:
        return prompt
    return prompt + "\n\n" + " ".join(extras)


def _apply_tint(layer: Image.Image, tint_rgba: tuple[int, int, int, int]) -> Image.Image:
    """Multiply the layer's RGB by `tint_rgba`, weighted by the tint's alpha.

    Alpha 255 = full multiply (image fully tinted). Alpha 0 = passthrough.
    The layer's own alpha channel is preserved unchanged.
    """
    r, g, b, a = tint_rgba
    strength = a / 255.0
    if strength <= 0.0:
        return layer
    base_rgb = layer.convert("RGB")
    tint_solid = Image.new("RGB", layer.size, (r, g, b))
    multiplied = ImageChops.multiply(base_rgb, tint_solid)
    blended = Image.blend(base_rgb, multiplied, strength)
    return Image.merge("RGBA", (*blended.split(), layer.getchannel("A")))


def _resolve_texture_rotation(rotation_spec: Any, seed_value: int) -> float:
    """Same shape as asset randomization.rotation, but returns one stable angle."""
    if rotation_spec is None or rotation_spec is False:
        return 0.0
    if rotation_spec is True:
        opts = [0.0, 90.0, 180.0, 270.0]
        return opts[seed_value % len(opts)]
    if isinstance(rotation_spec, list):
        opts = [float(v) for v in rotation_spec]
        return opts[seed_value % len(opts)] if opts else 0.0
    if isinstance(rotation_spec, (int, float)):
        return float(rotation_spec)
    return 0.0


def _apply_opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return img
    out = img.copy()
    alpha = out.getchannel("A").point(lambda p: int(p * opacity))
    out.putalpha(alpha)
    return out


def _stable_hash(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


def _choose_rotation(rotation_spec: Any, seed_value: int) -> float:
    if not rotation_spec:
        return 0.0
    if isinstance(rotation_spec, list):
        values = [float(v) for v in rotation_spec]
        return values[seed_value % len(values)] if values else 0.0
    if rotation_spec is True:
        values = [0.0, 90.0, 180.0, 270.0]
        return values[seed_value % len(values)]
    return 0.0


def _choose_jitter_factor(amount: Any, seed_value: int, *, shift: int) -> float:
    if not amount:
        return 1.0
    amt = abs(float(amount))
    raw = ((seed_value >> shift) & 1023) / 1023.0
    centered = (raw * 2.0) - 1.0
    return 1.0 + (centered * amt)


def _iter_polygons(geom: BaseGeometry) -> Iterable[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, (GeometryCollection,)):
        polys: list[Polygon] = []
        for g in geom.geoms:
            polys.extend(_iter_polygons(g))
        return polys
    return []


def _iter_lines(geom: BaseGeometry) -> Iterable[BaseGeometry]:
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        lines: list[BaseGeometry] = []
        for g in geom.geoms:
            lines.extend(_iter_lines(g))
        return lines
    return []


def _draw_polygon(draw: ImageDraw.ImageDraw, poly: Polygon, fill: Any) -> None:
    exterior = [(x, y) for x, y in poly.exterior.coords]
    draw.polygon(exterior, fill=fill)
    for hole in poly.interiors:
        coords = [(x, y) for x, y in hole.coords]
        draw.polygon(coords, fill=0)


def _offset_line(line: BaseGeometry, offset: float) -> BaseGeometry:
    if abs(offset) < 1e-6:
        return line
    side = "left" if offset > 0 else "right"
    shifted = line.parallel_offset(abs(offset), side=side, join_style=2)
    if shifted.geom_type == "MultiLineString":
        longest = max(shifted.geoms, key=lambda g: g.length, default=None)
        return longest if longest is not None else GeometryCollection()
    return shifted
