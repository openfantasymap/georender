"""Tests for OpenRouter-backed rendering.

Nothing here touches the network: `httpx.post` is monkeypatched everywhere. The
tests that matter most are the grounding ones — they decode the control image
that was actually put on the wire and assert it carries the real geometry, which
is the whole premise of the feature.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from georender_service.engine import GeoRenderer, RenderContext
from georender_service.openrouter import (
    OpenRouterClient,
    OpenRouterConfig,
    OpenRouterError,
    _extract_image_bytes,
)

from .conftest import PNG_SIGNATURE, make_png


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generated_png(color=(200, 60, 40, 255), w=64, h=64) -> bytes:
    return make_png(width=w, height=h, color=color)


def _openrouter_response(png: bytes) -> dict:
    """The documented image-output shape: message.images[].image_url.url."""
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "images": [{"type": "image_url", "image_url": {"url": uri}}],
                }
            }
        ]
    }


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")


def _mock_post(monkeypatch, png: bytes | None = None, payload=None, status_code=200):
    """Patch httpx.post and return the list that collects request bodies."""
    calls: list[dict] = []
    body = payload if payload is not None else _openrouter_response(png or _generated_png())

    def fake_post(url, *args, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return _Resp(body, status_code=status_code)

    import httpx as _httpx

    monkeypatch.setattr(_httpx, "post", fake_post)
    return calls


def _control_images_from(call: dict) -> list[Image.Image]:
    """Decode every input image the renderer attached to a request."""
    images = []
    for part in call["json"]["messages"][0]["content"]:
        if part.get("type") != "image_url":
            continue
        _, _, b64 = part["image_url"]["url"].partition(",")
        images.append(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA"))
    return images


def _prompt_from(call: dict) -> str:
    for part in call["json"]["messages"][0]["content"]:
        if part.get("type") == "text":
            return part["text"]
    return ""


POLY_FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"kind": "water", "name": "Test Lake"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10.1, 44.1], [10.9, 44.1], [10.9, 44.9], [10.1, 44.9], [10.1, 44.1]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "road"},
            "geometry": {"type": "LineString", "coordinates": [[10.2, 44.2], [10.8, 44.8]]},
        },
    ],
}


def _tile_for_lonlat(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Tile column/row containing a lon/lat, so tests can target real data."""
    from georender_service.geometry import WEB_MERCATOR_HALF, lonlat_to_mercator

    x, y = lonlat_to_mercator(lon, lat)
    span = (2 * WEB_MERCATOR_HALF) / (2**z)
    return int((x + WEB_MERCATOR_HALF) // span), int((WEB_MERCATOR_HALF - y) // span)


def _ai_ruleset(symbolizer_overrides: dict | None = None) -> dict:
    symbolizer = {"type": "ai_image", "model": "test/model"}
    symbolizer.update(symbolizer_overrides or {})
    return {
        "name": "ai",
        "background": "#00000000",
        "rules": [{"name": "basemap", "z_index": 0, "symbolizer": symbolizer}],
    }


@pytest.fixture()
def ai_renderer(tmp_ruleset_dir, tmp_assets, tmp_path, monkeypatch):
    """GeoRenderer with a key in the environment and a private cache dir."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def _build(symbolizer_overrides: dict | None = None, name: str = "ai"):
        (tmp_ruleset_dir / f"{name}.json").write_text(
            json.dumps(_ai_ruleset(symbolizer_overrides)), encoding="utf-8"
        )
        return GeoRenderer(tmp_ruleset_dir, tmp_assets, cache_dir=tmp_path / "cache")

    return _build


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_env_key_beats_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "openrouter.json"
    cfg_path.write_text(json.dumps({"api_key": "from-file", "model": "acme/painter"}), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")

    config = OpenRouterConfig.load(cfg_path)
    assert config.api_key == "from-env"
    # …but the file still supplies the taste-level defaults.
    assert config.model == "acme/painter"


def test_config_falls_back_to_file_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg_path = tmp_path / "openrouter.json"
    cfg_path.write_text(json.dumps({"api_key": "from-file"}), encoding="utf-8")
    assert OpenRouterConfig.load(cfg_path).api_key == "from-file"


def test_config_custom_api_key_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("MY_KEY", "scoped")
    cfg_path = tmp_path / "openrouter.json"
    cfg_path.write_text(json.dumps({"api_key_env": "MY_KEY"}), encoding="utf-8")
    assert OpenRouterConfig.load(cfg_path).api_key == "scoped"


def test_config_missing_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = OpenRouterConfig.load(tmp_path / "nope.json")
    assert config.api_key is None
    assert config.model  # default model still set


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_extract_image_from_message_images():
    png = _generated_png()
    assert _extract_image_bytes(_openrouter_response(png)) == png


def test_extract_image_from_content_parts():
    """Some models route the image through message.content[] instead."""
    png = _generated_png()
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    payload = {
        "choices": [
            {"message": {"content": [{"type": "image_url", "image_url": {"url": uri}}]}}
        ]
    }
    assert _extract_image_bytes(payload) == png


def test_extract_image_from_openai_images_shape():
    """Proxies that mimic the OpenAI images endpoint."""
    png = _generated_png()
    payload = {"data": [{"b64_json": base64.b64encode(png).decode("ascii")}]}
    assert _extract_image_bytes(payload) == png


def test_extract_image_returns_none_for_text_only():
    assert _extract_image_bytes({"choices": [{"message": {"content": "I can't do that"}}]}) is None


# ---------------------------------------------------------------------------
# Client behaviour
# ---------------------------------------------------------------------------


def test_client_caches_generation_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    png = _generated_png()
    calls = _mock_post(monkeypatch, png)

    client = OpenRouterClient(cache_dir=tmp_path / "cache")
    first = client.generate_image("paint a lake", model="test/model")
    # A fresh client proves it came off disk, not the in-process memo.
    second = OpenRouterClient(cache_dir=tmp_path / "cache").generate_image(
        "paint a lake", model="test/model"
    )
    assert first == second == png
    assert len(calls) == 1


def test_client_serves_cache_without_api_key(tmp_path, monkeypatch):
    """Once generated, a render must stay reproducible with no key present."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    png = _generated_png()
    _mock_post(monkeypatch, png)
    OpenRouterClient(cache_dir=tmp_path / "cache").generate_image("x", model="test/model")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    offline = OpenRouterClient(cache_dir=tmp_path / "cache")
    assert offline.config.api_key is None
    assert offline.generate_image("x", model="test/model") == png


def test_client_without_key_and_without_cache_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = OpenRouterClient(cache_dir=tmp_path / "cache")
    with pytest.raises(OpenRouterError, match="no API key"):
        client.generate_image("x", model="test/model")


def test_client_cache_false_always_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    calls = _mock_post(monkeypatch)
    client = OpenRouterClient(cache_dir=tmp_path / "cache")
    client.generate_image("x", model="test/model", cache=False)
    client.generate_image("x", model="test/model", cache=False)
    assert len(calls) == 2


def test_client_surfaces_refusal_text(tmp_path, monkeypatch):
    """A refusal is a 200 with prose — the message must reach the operator."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _mock_post(monkeypatch, payload={"choices": [{"message": {"content": "I won't draw that"}}]})
    client = OpenRouterClient(cache_dir=tmp_path / "cache")
    with pytest.raises(OpenRouterError, match="I won't draw that"):
        client.generate_image("x", model="test/model")


def test_client_surfaces_http_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _mock_post(monkeypatch, payload={"error": {"message": "insufficient credits"}}, status_code=402)
    client = OpenRouterClient(cache_dir=tmp_path / "cache")
    with pytest.raises(OpenRouterError, match="402"):
        client.generate_image("x", model="test/model")


def test_client_sends_auth_and_modalities(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    calls = _mock_post(monkeypatch)
    OpenRouterClient(cache_dir=tmp_path / "cache").generate_image("x", model="test/model")
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-or-test"
    assert calls[0]["json"]["modalities"] == ["image", "text"]
    assert calls[0]["json"]["model"] == "test/model"


# ---------------------------------------------------------------------------
# ai_image: grounding
# ---------------------------------------------------------------------------


def test_ai_image_composites_generated_pixels(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png((10, 200, 90, 255)))
    renderer = ai_renderer()
    data = renderer.render_png(POLY_FEATURES, "ai", width=64, height=64)

    assert data[:8] == PNG_SIGNATURE
    img = Image.open(io.BytesIO(data))
    assert any(px[3] > 0 for px in img.getdata())
    assert len(calls) == 1


def test_ai_image_sends_control_image_with_real_geometry(ai_renderer, monkeypatch):
    """The control image must carry the actual features, in the declared colours."""
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer(
        {
            "control": {
                "group_by": "kind",
                "groups": {
                    "water": {"color": "#ff0000", "describe": "a shallow lagoon"},
                    "road": {"color": "#00ff00", "describe": "a gravel track"},
                },
                "background": "#000000",
            }
        }
    )
    # padding leaves a margin around the fitted features so the background shows.
    renderer.render_png(POLY_FEATURES, "ai", width=128, height=128, padding_px=16)

    controls = _control_images_from(calls[0])
    assert len(controls) == 1
    colors = {px[:3] for px in controls[0].getdata()}
    # The polygon and the line were both drawn, in exactly the declared colours.
    assert (255, 0, 0) in colors
    assert (0, 255, 0) in colors
    assert (0, 0, 0) in colors  # background survives around them


BACKDROP_FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {   # a viewport-sized backdrop, first in source order
            "type": "Feature",
            "properties": {"kind": "backdrop"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10.0, 44.0], [11.0, 44.0], [11.0, 45.0], [10.0, 45.0], [10.0, 44.0]]],
            },
        },
        {   # a small unit that must survive on top of it
            "type": "Feature",
            "properties": {"kind": "dune"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10.4, 44.4], [10.6, 44.4], [10.6, 44.6], [10.4, 44.6], [10.4, 44.4]]],
            },
        },
    ],
}


def test_control_draws_large_features_first(ai_renderer, monkeypatch):
    """One rule paints every feature in one pass, so it has no per-rule z_index
    to lean on. Biggest-first ordering keeps a backdrop from burying the units
    inside it."""
    from PIL import ImageColor

    calls = _mock_post(monkeypatch)
    control = {
        "group_by": "kind",
        "groups": {"backdrop": {"color": "#eb7d0e"}, "dune": {"color": "#ff3b30"}},
    }
    ai_renderer({"control": control}).render_png(
        BACKDROP_FEATURES, "ai", width=128, height=128, padding_px=0
    )
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    assert ImageColor.getcolor("#ff3b30", "RGB") in colors  # the dune survived
    assert ImageColor.getcolor("#eb7d0e", "RGB") in colors  # backdrop still there


def test_control_draw_order_source_can_be_restored(ai_renderer, monkeypatch):
    from PIL import ImageColor

    calls = _mock_post(monkeypatch)
    control = {
        "group_by": "kind",
        "draw_order": "source",
        "groups": {"backdrop": {"color": "#eb7d0e"}, "dune": {"color": "#ff3b30"}},
    }
    ai_renderer({"control": control}).render_png(
        BACKDROP_FEATURES, "ai", width=128, height=128, padding_px=0
    )
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    # Source order draws the backdrop first, so the dune still lands on top here;
    # the knob exists for data where the fetched order is already meaningful.
    assert ImageColor.getcolor("#ff3b30", "RGB") in colors


def test_control_lines_and_points_stay_above_polygons(ai_renderer, monkeypatch):
    """Zero-area geometries sort last, so a road is never swallowed by a lake."""
    from PIL import ImageColor

    calls = _mock_post(monkeypatch)
    control = {
        "group_by": "kind",
        "groups": {"water": {"color": "#0a84ff"}, "road": {"color": "#34c759"}},
    }
    ai_renderer({"control": control}).render_png(
        POLY_FEATURES, "ai", width=128, height=128, padding_px=0
    )
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    assert ImageColor.getcolor("#34c759", "RGB") in colors


def test_ai_image_prompt_always_carries_the_grounding_preamble(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    ai_renderer().render_png(POLY_FEATURES, "ai", width=64, height=64)
    prompt = _prompt_from(calls[0])
    assert "control image" in prompt
    assert "do not move" in prompt.lower()


def test_ai_image_legend_matches_the_pixels(ai_renderer, monkeypatch):
    """Prompt legend and control image cannot disagree: same resolved colours."""
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer(
        {
            "control": {
                "group_by": "kind",
                "groups": {"water": {"color": "#ff0000", "describe": "a shallow lagoon"}},
            }
        }
    )
    renderer.render_png(POLY_FEATURES, "ai", width=64, height=64, padding_px=0)

    prompt = _prompt_from(calls[0])
    assert "#ff0000 -> a shallow lagoon" in prompt
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    assert (255, 0, 0) in colors


def test_ai_image_legend_is_opt_in(ai_renderer, monkeypatch):
    """With no descriptions the legend block stays out of the prompt."""
    calls = _mock_post(monkeypatch)
    ai_renderer({"control": {"group_by": "kind"}}).render_png(
        POLY_FEATURES, "ai", width=64, height=64
    )
    assert "Colour legend" not in _prompt_from(calls[0])


def test_ai_image_scene_description_is_opt_in(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    ai_renderer().render_png(POLY_FEATURES, "ai", width=64, height=64)
    assert "Scene facts" not in _prompt_from(calls[0])


def test_ai_image_scene_description_uses_real_measurements(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer(
        {"describe_scene": True, "control": {"group_by": "kind", "name_property": "name"}}
    )
    renderer.render_png(POLY_FEATURES, "ai", width=64, height=64, padding_px=0)

    prompt = _prompt_from(calls[0])
    assert "Scene facts" in prompt
    assert "m per pixel" in prompt
    assert "EPSG:4326" in prompt
    assert "north is up" in prompt
    # Counts and names come from the features themselves.
    assert "1 x water" in prompt
    assert "Test Lake" in prompt


def test_ai_image_style_prompt_is_appended(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    ai_renderer({"prompt": "1930s hand-inked survey"}).render_png(
        POLY_FEATURES, "ai", width=64, height=64
    )
    assert "1930s hand-inked survey" in _prompt_from(calls[0])


def test_ai_image_filter_selects_which_features_ground_it(ai_renderer, monkeypatch):
    """A rule filter narrows the control render, like any other symbolizer."""
    calls = _mock_post(monkeypatch)
    control = {
        "group_by": "kind",
        "groups": {"water": {"color": "#ff0000"}, "road": {"color": "#00ff00"}},
    }
    renderer = ai_renderer({"control": control})
    # Re-write the ruleset with a filter that admits only the water polygon.
    ruleset = _ai_ruleset({"control": control})
    ruleset["rules"][0]["filter"] = {"kind": "water"}
    (renderer.rules.base_dir / "ai.json").write_text(json.dumps(ruleset), encoding="utf-8")

    renderer.render_png(POLY_FEATURES, "ai", width=128, height=128, padding_px=0)
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    assert (255, 0, 0) in colors
    assert (0, 255, 0) not in colors


def test_auto_control_colour_is_derived_from_the_group_key():
    """Undeclared groups take a colour from the key, not from iteration order —
    otherwise neighbouring anchor cells, which see different subsets of layers,
    would paint the same layer different colours and repaint it differently."""
    from georender_service.engine import _auto_control_color

    assert _auto_control_color("Dossi") == _auto_control_color("Dossi")
    assert _auto_control_color("Dossi") != _auto_control_color("Paleoalvei")
    assert len(_auto_control_color("Dossi")) == 7


def test_ai_image_auto_colours_land_in_the_control_image(ai_renderer, monkeypatch):
    """The colour the legend would name is the colour actually drawn."""
    from PIL import ImageColor

    from georender_service.engine import _auto_control_color

    calls = _mock_post(monkeypatch)
    ai_renderer({"control": {"group_by": "kind"}}).render_png(
        POLY_FEATURES, "ai", width=64, height=64, padding_px=16
    )
    colors = {px[:3] for px in _control_images_from(calls[0])[0].getdata()}
    assert ImageColor.getcolor(_auto_control_color("water"), "RGB") in colors


def test_ai_image_replace_canvas_overwrites_lower_rules(ai_renderer, monkeypatch):
    _mock_post(monkeypatch, _generated_png((7, 9, 11, 255)))
    renderer = ai_renderer({"replace_canvas": True})
    data = renderer.render_png(POLY_FEATURES, "ai", width=32, height=32)
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    assert set(img.getdata()) == {(7, 9, 11, 255)}


def test_ai_image_opacity_is_applied(ai_renderer, monkeypatch):
    _mock_post(monkeypatch, _generated_png((255, 255, 255, 255)))
    renderer = ai_renderer({"opacity": 0.5, "replace_canvas": True})
    data = renderer.render_png(POLY_FEATURES, "ai", width=16, height=16)
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    assert all(px[3] < 200 for px in img.getdata())


def test_ai_image_downscales_control_to_max_side(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    ai_renderer({"max_side_px": 96}).render_png(POLY_FEATURES, "ai", width=512, height=512)
    control = _control_images_from(calls[0])[0]
    assert max(control.size) == 96


# ---------------------------------------------------------------------------
# ai_image: failure modes
# ---------------------------------------------------------------------------


def test_ai_image_missing_key_warns_and_keeps_render_alive(
    tmp_ruleset_dir, tmp_assets, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_ruleset_dir / "ai.json").write_text(json.dumps(_ai_ruleset()), encoding="utf-8")
    renderer = GeoRenderer(tmp_ruleset_dir, tmp_assets, cache_dir=tmp_path / "cache")

    data = renderer.render_png(POLY_FEATURES, "ai", width=32, height=32)
    assert data[:8] == PNG_SIGNATURE
    err = capsys.readouterr().err
    assert "ai_image rule 'basemap'" in err
    assert "no API key" in err


def test_ai_image_network_failure_keeps_render_alive(ai_renderer, monkeypatch, capsys):
    import httpx as _httpx

    def raising_post(*a, **kw):
        raise _httpx.ConnectError("offline")

    monkeypatch.setattr(_httpx, "post", raising_post)
    data = ai_renderer().render_png(POLY_FEATURES, "ai", width=32, height=32)
    assert data[:8] == PNG_SIGNATURE
    assert "network error" in capsys.readouterr().err


def test_ai_image_undecodable_payload_warns_and_skips(ai_renderer, monkeypatch, capsys):
    payload = {
        "choices": [
            {
                "message": {
                    "images": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(b"not an image").decode("ascii")
                            },
                        }
                    ]
                }
            }
        ]
    }
    _mock_post(monkeypatch, payload=payload)
    data = ai_renderer().render_png(POLY_FEATURES, "ai", width=32, height=32)
    assert data[:8] == PNG_SIGNATURE
    assert "not a decodable image" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ai_image: anchor grid
# ---------------------------------------------------------------------------


def _render_tile(renderer, ruleset, z, x, y, *, tile_size=64, features=None):
    from georender_service.geometry import ensure_mercator, load_geom, mercator_tile_bounds, viewport_from_bounds

    feats = features if features is not None else POLY_FEATURES["features"]
    geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in feats]
    viewport = viewport_from_bounds(mercator_tile_bounds(x, y, z), width=tile_size, height=tile_size)
    context = RenderContext(
        tile=(z, x, y),
        source_revision="rev1",
        source_crs="EPSG:4326",
        fetch_features=lambda bounds: feats,
    )
    return renderer.render_tile_image(feats, geoms, ruleset, viewport, context=context)


def test_anchor_grid_shares_one_generation_across_tiles(ai_renderer, monkeypatch):
    """Four sibling tiles inside one anchor cell must cost exactly one call."""
    calls = _mock_post(monkeypatch, _generated_png(w=256, h=256))
    renderer = ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256})

    for x, y in ((8, 8), (9, 8), (8, 9), (9, 9)):
        img = _render_tile(renderer, "ai", 10, x, y)
        assert img.size == (64, 64)
    assert len(calls) == 1


def test_anchor_grid_regenerates_for_a_different_cell(ai_renderer, monkeypatch):
    """`describe_scene` puts the cell's own extent in the prompt, so two cells
    are genuinely different requests rather than two names for one image."""
    calls = _mock_post(monkeypatch, _generated_png(w=256, h=256))
    renderer = ai_renderer(
        {"anchor_zoom_delta": 2, "anchor_size_px": 256, "describe_scene": True}
    )

    _render_tile(renderer, "ai", 10, 8, 8)   # anchor 8/2/2
    _render_tile(renderer, "ai", 10, 40, 8)  # anchor 8/10/2
    assert len(calls) == 2
    assert _prompt_from(calls[0]) != _prompt_from(calls[1])


def test_anchor_cache_survives_a_new_renderer(ai_renderer, monkeypatch):
    """The anchor cache is keyed on identity, so a cold process reuses it
    without re-fetching features or re-billing."""
    calls = _mock_post(monkeypatch, _generated_png(w=256, h=256))
    ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256})
    _render_tile(ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256}), "ai", 10, 8, 8)
    _render_tile(ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256}), "ai", 10, 9, 9)
    assert len(calls) == 1


def test_anchor_cache_busts_on_source_revision(ai_renderer, monkeypatch):
    """A new source revision must re-derive the control render.

    It only re-bills when the data actually changed — identical control pixels
    still collapse onto the content-addressed cache — so this drives a real data
    change alongside the revision bump.
    """
    from georender_service.geometry import ensure_mercator, load_geom, mercator_tile_bounds, viewport_from_bounds

    calls = _mock_post(monkeypatch, _generated_png(w=256, h=256))
    renderer = ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256})
    # Target the tile that actually holds the sample features, so deleting one of
    # them changes the control render — over empty ocean both revisions would
    # produce the same blank cell and collapse onto the content cache.
    tx, ty = _tile_for_lonlat(10.5, 44.5, 10)
    viewport = viewport_from_bounds(mercator_tile_bounds(tx, ty, 10), width=64, height=64)

    revisions = {
        "rev1": POLY_FEATURES["features"],
        "rev2": POLY_FEATURES["features"][1:],  # the lake was deleted upstream
    }
    for revision, feats in revisions.items():
        geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in feats]
        renderer.render_tile_image(
            feats,
            geoms,
            "ai",
            viewport,
            context=RenderContext(
                tile=(10, tx, ty),
                source_revision=revision,
                source_crs="EPSG:4326",
                fetch_features=lambda bounds, _f=feats: _f,
            ),
        )
    assert len(calls) == 2

    # Re-rendering an unchanged revision comes straight off the anchor cache.
    feats = revisions["rev1"]
    geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in feats]
    renderer.render_tile_image(
        feats, geoms, "ai", viewport,
        context=RenderContext(
            tile=(10, tx, ty), source_revision="rev1", source_crs="EPSG:4326",
            fetch_features=lambda bounds: feats,
        ),
    )
    assert len(calls) == 2


def test_anchor_uses_the_wider_feature_fetch(ai_renderer, monkeypatch):
    """The anchor cell is much larger than the tile, so the renderer must ask
    for its own features rather than reuse the tile's slice."""
    calls = _mock_post(monkeypatch, _generated_png(w=256, h=256))
    requested: list[tuple] = []
    renderer = ai_renderer({"anchor_zoom_delta": 3, "anchor_size_px": 256})

    from georender_service.geometry import ensure_mercator, load_geom, mercator_tile_bounds, viewport_from_bounds

    feats = POLY_FEATURES["features"]
    geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in feats]
    tile_bounds = mercator_tile_bounds(8, 8, 10)
    viewport = viewport_from_bounds(tile_bounds, width=64, height=64)

    def _fetch(bounds):
        requested.append(bounds)
        return feats

    renderer.render_tile_image(
        feats, geoms, "ai", viewport,
        context=RenderContext(tile=(10, 8, 8), source_revision="r", fetch_features=_fetch),
    )
    assert len(requested) == 1
    # The fetched window is the anchor cell (z7), strictly wider than the tile.
    assert requested[0][2] - requested[0][0] > tile_bounds[2] - tile_bounds[0]
    assert len(calls) == 1


def test_anchor_disabled_generates_per_tile(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer({"anchor": False, "describe_scene": True})
    _render_tile(renderer, "ai", 10, 8, 8)
    _render_tile(renderer, "ai", 10, 9, 8)
    assert len(calls) == 2


def test_image_png_never_anchors(ai_renderer, monkeypatch):
    """No tile address means nothing to snap to — render the viewport directly."""
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer({"anchor_zoom_delta": 4})
    renderer.render_png(POLY_FEATURES, "ai", width=64, height=64)
    control = _control_images_from(calls[0])[0]
    assert control.size == (64, 64)


def test_anchor_warns_when_canvas_control_is_unavailable(ai_renderer, monkeypatch, capsys):
    _mock_post(monkeypatch, _generated_png(w=256, h=256))
    renderer = ai_renderer(
        {"anchor_zoom_delta": 2, "anchor_size_px": 256, "control": {"source": "canvas"}}
    )
    _render_tile(renderer, "ai", 10, 8, 8)
    err = capsys.readouterr().err
    assert "not available under anchor-grid generation" in err


def test_missing_fetcher_warns_but_still_renders(ai_renderer, monkeypatch, capsys):
    from georender_service.geometry import ensure_mercator, load_geom, mercator_tile_bounds, viewport_from_bounds

    _mock_post(monkeypatch, _generated_png(w=256, h=256))
    renderer = ai_renderer({"anchor_zoom_delta": 2, "anchor_size_px": 256})
    feats = POLY_FEATURES["features"]
    geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in feats]
    viewport = viewport_from_bounds(mercator_tile_bounds(8, 8, 10), width=64, height=64)

    renderer.render_tile_image(
        feats, geoms, "ai", viewport,
        context=RenderContext(tile=(10, 8, 8), source_revision="r"),
    )
    assert "no feature fetcher" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ai_image: control.source variants
# ---------------------------------------------------------------------------


def test_control_source_both_sends_two_images(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer({"control": {"source": "both", "group_by": "kind"}})
    renderer.render_png(POLY_FEATURES, "ai", width=64, height=64)
    assert len(_control_images_from(calls[0])) == 2


def test_control_source_canvas_sends_the_scene_so_far(ai_renderer, monkeypatch):
    calls = _mock_post(monkeypatch)
    renderer = ai_renderer({"control": {"source": "canvas"}})
    renderer.render_png(POLY_FEATURES, "ai", width=64, height=64)
    assert len(_control_images_from(calls[0])) == 1


# ---------------------------------------------------------------------------
# Generated assets (openrouter:// file scheme)
# ---------------------------------------------------------------------------


def _asset_registry(asset: dict) -> dict:
    return {"collections": {"gen": {"thing": asset}}}


def _asset_ruleset(symbolizer: dict) -> dict:
    return {
        "name": "gen",
        "background": "#00000000",
        "asset_collections": {"gen": "gen"},
        "rules": [
            {
                "name": "stamps",
                "z_index": 1,
                "geometry": ["Point", "MultiPoint"],
                "filter": {},
                "symbolizer": symbolizer,
            }
        ],
    }


POINT_FEATURES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"kind": "shrine", "name": "North Shrine"},
            "geometry": {"type": "Point", "coordinates": [10.3, 44.3]},
        },
        {
            "type": "Feature",
            "properties": {"kind": "shrine", "name": "South Shrine"},
            "geometry": {"type": "Point", "coordinates": [10.7, 44.7]},
        },
    ],
}


@pytest.fixture()
def gen_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def _build(asset: dict, symbolizer: dict | None = None):
        assets_dir = tmp_path / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / "assets.json").write_text(json.dumps(_asset_registry(asset)), encoding="utf-8")
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir(exist_ok=True)
        symb = symbolizer or {"type": "icon", "asset": "gen.thing", "size_px": 12}
        (rules_dir / "gen.json").write_text(json.dumps(_asset_ruleset(symb)), encoding="utf-8")
        return GeoRenderer(rules_dir, assets_dir, cache_dir=tmp_path / "cache")

    return _build


def test_generated_asset_is_stamped(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png((240, 30, 30, 255), w=32, h=32))
    renderer = gen_assets(
        {"file": "openrouter://test/model", "kind": "icon", "prompt": "a small stone shrine"}
    )
    data = renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    assert any(px[3] > 0 for px in img.getdata())
    assert len(calls) == 1
    assert "a small stone shrine" in _prompt_from(calls[0])


def test_generated_asset_prompt_uses_real_feature_properties(gen_assets, monkeypatch):
    """`{name}` and friends resolve against the feature actually being drawn."""
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets(
        {
            "file": "openrouter://test/model",
            "kind": "icon",
            "prompt": "map symbol for {name}, a {kind}",
            "vary_by_seed": True,
        }
    )
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)

    prompts = [_prompt_from(c) for c in calls]
    assert any("map symbol for North Shrine, a shrine" in p for p in prompts)
    assert any("map symbol for South Shrine, a shrine" in p for p in prompts)


def test_generated_asset_unknown_placeholder_collapses(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets(
        {"file": "openrouter://test/model", "prompt": "a shrine {nosuchprop} on a hill"}
    )
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert "a shrine  on a hill" in _prompt_from(calls[0])


def test_generated_asset_shared_prompt_costs_one_call(gen_assets, monkeypatch):
    """Two features, one prompt, one generation — the default for textures."""
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets({"file": "openrouter://test/model", "prompt": "a stone shrine"})
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert len(calls) == 1


def test_generated_asset_tileable_adds_seam_instruction(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets(
        {"file": "openrouter://test/model", "prompt": "dune sand", "tileable": True}
    )
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert "tile seamlessly" in _prompt_from(calls[0])


def test_generated_asset_transparent_adds_alpha_instruction(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets(
        {"file": "openrouter://test/model", "prompt": "a shrine", "transparent": True}
    )
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert "transparent background" in _prompt_from(calls[0])


def test_generated_asset_without_prompt_errors(gen_assets, monkeypatch):
    _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets({"file": "openrouter://test/model"})
    with pytest.raises(ValueError, match="no `prompt`"):
        renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)


def test_generated_asset_variant_set_generates_each_variant(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets(
        {
            "kind": "variant_set",
            "prompt": "a wayside shrine",
            "variants": [
                {"file": "openrouter://test/model", "prompt": "a ruined shrine", "weight": 1},
                {"file": "openrouter://test/model", "prompt": "an intact shrine", "weight": 1},
            ],
        }
    )
    renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    prompts = {_prompt_from(c) for c in calls}
    # Whatever the deterministic pick, the variant's prompt wins over the parent's.
    assert prompts
    assert all("shrine" in p for p in prompts)
    assert not any("a wayside shrine" == p for p in prompts)


def test_generated_asset_reuses_disk_cache_across_renderers(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    asset = {"file": "openrouter://test/model", "prompt": "a stone shrine"}
    gen_assets(asset).render_png(POINT_FEATURES, "gen", width=64, height=64)
    gen_assets(asset).render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert len(calls) == 1


def test_generated_asset_with_extra_body_still_hits_the_cache(gen_assets, monkeypatch):
    """The path lookup has to rebuild the same cache key generate_image used —
    `extra_body` is part of it, so forgetting it would regenerate every render."""
    calls = _mock_post(monkeypatch, _generated_png(w=32, h=32))
    asset = {
        "file": "openrouter://test/model",
        "prompt": "a stone shrine",
        "extra_body": {"temperature": 0.2},
    }
    gen_assets(asset).render_png(POINT_FEATURES, "gen", width=64, height=64)
    gen_assets(asset).render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert len(calls) == 1
    assert calls[0]["json"]["temperature"] == 0.2


def test_generated_asset_uncached_still_materializes(gen_assets, monkeypatch, tmp_path):
    """`cache: false` writes nowhere permanent, but Image.open still needs a file
    — and it must not land in the user's assets/ directory."""
    _mock_post(monkeypatch, _generated_png(w=32, h=32))
    renderer = gen_assets({"file": "openrouter://test/model", "prompt": "a shrine", "cache": False})
    data = renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    assert data[:8] == PNG_SIGNATURE
    assert not (tmp_path / "assets" / ".generated").exists()
    assert (tmp_path / "cache" / "openrouter" / "adhoc").exists()


def test_generated_asset_failure_surfaces_a_clear_error(gen_assets, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    renderer = gen_assets({"file": "openrouter://test/model", "prompt": "a shrine"})
    with pytest.raises(FileNotFoundError, match="Failed to generate asset"):
        renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)


def test_generated_tileable_texture_is_usable_as_a_stamp(gen_assets, monkeypatch):
    calls = _mock_post(monkeypatch, _generated_png((90, 140, 60, 255), w=32, h=32))
    renderer = gen_assets(
        {"file": "openrouter://test/model", "prompt": "dry scrub on sand", "tileable": True},
        symbolizer={"type": "icon", "asset": "gen.thing", "size_px": 16},
    )
    data = renderer.render_png(POINT_FEATURES, "gen", width=64, height=64)
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    assert any(px[3] > 0 for px in img.getdata())
    assert len(calls) == 1
