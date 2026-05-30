from __future__ import annotations

import json

import pytest

from georender_service.rules import RulesetError, RulesetStore, feature_matches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feature(geom_type: str, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": []},
        "properties": props,
    }


def _rule(geom_types: list, filter_: dict = None, **extra) -> dict:
    return {
        "geometry": geom_types,
        "filter": filter_ or {},
        "symbolizer": {"type": "polygon_fill"},
        **extra,
    }


# ---------------------------------------------------------------------------
# feature_matches — geometry gating
# ---------------------------------------------------------------------------


def test_feature_matches_wrong_geometry_type():
    rule = _rule(["Point"])
    assert not feature_matches(rule, _feature("Polygon", kind="water"), "Polygon")


def test_feature_matches_correct_geometry_type():
    rule = _rule(["Polygon"])
    assert feature_matches(rule, _feature("Polygon", kind="water"), "Polygon")


def test_feature_matches_multi_geometry_types():
    rule = _rule(["Polygon", "MultiPolygon"])
    assert feature_matches(rule, _feature("MultiPolygon"), "MultiPolygon")


# ---------------------------------------------------------------------------
# feature_matches — filter operators
# ---------------------------------------------------------------------------


def test_feature_matches_no_filter():
    rule = _rule(["Point"], filter_={})
    assert feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_equality_hit():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_equality_miss():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert not feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_equality_missing_prop():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert not feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_in_operator_hit():
    rule = _rule(["Point"], filter_={"kind": {"in": ["city", "town"]}})
    assert feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_in_operator_miss():
    rule = _rule(["Point"], filter_={"kind": {"in": ["city", "town"]}})
    assert not feature_matches(rule, _feature("Point", kind="village"), "Point")


def test_feature_matches_not_in_operator_hit():
    rule = _rule(["Point"], filter_={"kind": {"not_in": ["city"]}})
    assert feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_not_in_operator_miss():
    rule = _rule(["Point"], filter_={"kind": {"not_in": ["city"]}})
    assert not feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_exists_true():
    rule = _rule(["Point"], filter_={"name": {"exists": True}})
    assert feature_matches(rule, _feature("Point", name="Alpha"), "Point")


def test_feature_matches_exists_true_miss():
    rule = _rule(["Point"], filter_={"name": {"exists": True}})
    assert not feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_exists_false():
    rule = _rule(["Point"], filter_={"name": {"exists": False}})
    assert feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_gte_hit():
    rule = _rule(["Point"], filter_={"pop": {"gte": 1000}})
    assert feature_matches(rule, _feature("Point", pop=5000), "Point")


def test_feature_matches_gte_miss():
    rule = _rule(["Point"], filter_={"pop": {"gte": 1000}})
    assert not feature_matches(rule, _feature("Point", pop=500), "Point")


def test_feature_matches_lte_hit():
    rule = _rule(["Point"], filter_={"level": {"lte": 5}})
    assert feature_matches(rule, _feature("Point", level=3), "Point")


def test_feature_matches_lte_miss():
    rule = _rule(["Point"], filter_={"level": {"lte": 5}})
    assert not feature_matches(rule, _feature("Point", level=10), "Point")


def test_feature_matches_multiple_conditions_all_must_pass():
    rule = _rule(["Point"], filter_={"kind": "city", "pop": {"gte": 1000}})
    assert feature_matches(rule, _feature("Point", kind="city", pop=2000), "Point")
    assert not feature_matches(rule, _feature("Point", kind="city", pop=500), "Point")
    assert not feature_matches(rule, _feature("Point", kind="town", pop=2000), "Point")


# ---------------------------------------------------------------------------
# RulesetStore — load / list / revision
# ---------------------------------------------------------------------------


def test_ruleset_store_list_names(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    assert "test" in store.list_names()


def test_ruleset_store_load_returns_dict(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    data = store.load("test")
    assert isinstance(data, dict)
    assert "rules" in data


def test_ruleset_store_load_not_found_raises(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    with pytest.raises(RulesetError, match="not found"):
        store.load("nonexistent")


def test_ruleset_store_revision_is_string(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    rev = store.revision("test")
    assert isinstance(rev, str) and len(rev) == 16


def test_ruleset_store_revision_changes_on_edit(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    rev1 = store.revision("test")
    ruleset_path = tmp_ruleset_dir / "test.json"
    data = json.loads(ruleset_path.read_text())
    data["background"] = "#000000"
    ruleset_path.write_text(json.dumps(data))
    rev2 = store.revision("test")
    assert rev1 != rev2


def test_ruleset_store_revision_not_found_raises(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    with pytest.raises(RulesetError):
        store.revision("ghost")


# ---------------------------------------------------------------------------
# RulesetStore — legacy key normalization
# ---------------------------------------------------------------------------


def _write_ruleset(path, ruleset: dict) -> None:
    path.write_text(json.dumps(ruleset), encoding="utf-8")


def test_normalize_paint_to_symbolizer(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "paint": {"type": "polygon_fill", "fill": "#ff0000"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert "symbolizer" in data["rules"][0]
    assert "paint" not in data["rules"][0]


def test_normalize_z_to_z_index(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "z": 5,
                    "symbolizer": {"type": "polygon_fill"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert data["rules"][0]["z_index"] == 5


def test_normalize_where_to_filter(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "where": {"all": [{"field": "kind", "in": ["water"]}]},
                    "symbolizer": {"type": "polygon_fill"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert "filter" in data["rules"][0]
    assert "kind" in data["rules"][0]["filter"]


# ---------------------------------------------------------------------------
# RulesetStore — validation errors
# ---------------------------------------------------------------------------


def test_validate_missing_rules_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(tmp_path / "bad.json", {"background": "#fff"})
    with pytest.raises(RulesetError, match="rules"):
        store.load("bad")


def test_validate_bad_symbolizer_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {"rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "explode"}}]},
    )
    with pytest.raises(RulesetError, match="symbolizer"):
        store.load("bad")


def test_validate_bad_geometry_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {"rules": [{"geometry": ["Triangle"], "symbolizer": {"type": "polygon_fill"}}]},
    )
    with pytest.raises(RulesetError, match="geometry"):
        store.load("bad")


def test_validate_negative_edge_fade_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "symbolizer": {"type": "polygon_fill"},
                    "edge_fade": {"distance_px": -1},
                }
            ]
        },
    )
    with pytest.raises(RulesetError, match="edge_fade"):
        store.load("bad")


def test_validate_asset_collections_bad_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {
            "asset_collections": "not-a-list-or-dict",
            "rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}],
        },
    )
    with pytest.raises(RulesetError, match="asset_collections"):
        store.load("bad")


# ---------------------------------------------------------------------------
# RulesetStore — remote stubs ($remote)
# ---------------------------------------------------------------------------


_REMOTE_RULESET = {
    "name": "remote",
    "background": "#0a0a0a",
    "rules": [
        {
            "geometry": ["Polygon", "MultiPolygon"],
            "filter": {"kind": "water"},
            "symbolizer": {"type": "polygon_fill", "fill": "#1e90ff80"},
        }
    ],
}


def _install_fake_http(monkeypatch, routes):
    import httpx as _httpx

    class _Resp:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=_httpx.Request("GET", "https://example.invalid/"),
                    response=self,  # type: ignore[arg-type]
                )

    def fake_get(url, *args, **kwargs):
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                return _Resp(200, payload)
        return _Resp(404, b"")

    monkeypatch.setattr(_httpx, "get", fake_get)


def test_remote_stub_fetches_and_validates(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    _write_ruleset(
        rulesets_dir / "vt.json",
        {"$remote": "github://alice/world@HEAD/ruleset.json"},
    )

    payload = json.dumps(_REMOTE_RULESET).encode("utf-8")
    _install_fake_http(
        monkeypatch,
        {"https://cdn.jsdelivr.net/gh/alice/world@HEAD/ruleset.json": payload},
    )

    store = RulesetStore(rulesets_dir, cache_dir=cache_dir)
    data = store.load("vt")
    assert data["background"] == "#0a0a0a"
    assert data["rules"][0]["symbolizer"]["type"] == "polygon_fill"
    cached_files = list((cache_dir / "rulesets").iterdir())
    assert len(cached_files) == 1


def test_remote_stub_revision_changes_with_upstream(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    stub = {"$remote": "https://example.com/r.json"}
    _write_ruleset(rulesets_dir / "vt.json", stub)

    v1 = json.dumps(_REMOTE_RULESET).encode("utf-8")
    _install_fake_http(monkeypatch, {"https://example.com/r.json": v1})
    rev1 = RulesetStore(rulesets_dir, cache_dir=cache_dir).revision("vt")

    # Bust the cache and serve a different payload.
    (cache_dir / "rulesets").rename(cache_dir / "rulesets_old")
    altered = dict(_REMOTE_RULESET)
    altered["background"] = "#ff0000"
    v2 = json.dumps(altered).encode("utf-8")
    _install_fake_http(monkeypatch, {"https://example.com/r.json": v2})
    rev2 = RulesetStore(rulesets_dir, cache_dir=cache_dir).revision("vt")
    assert rev1 != rev2


def test_remote_stub_uses_in_memory_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    _write_ruleset(rulesets_dir / "vt.json", {"$remote": "https://example.com/r.json"})

    calls: list[str] = []
    import httpx as _httpx

    class _Resp:
        status_code = 200
        content = json.dumps(_REMOTE_RULESET).encode("utf-8")

        def raise_for_status(self):
            return None

    def counting_get(url, *a, **kw):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(_httpx, "get", counting_get)

    store = RulesetStore(rulesets_dir, cache_dir=cache_dir)
    store.load("vt")
    store.load("vt")
    store.revision("vt")
    # First call hits the network; subsequent calls must come from the cache.
    assert len(calls) == 1


def test_remote_stub_without_cache_dir_raises(tmp_path):
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    _write_ruleset(rulesets_dir / "vt.json", {"$remote": "https://example.com/r.json"})
    store = RulesetStore(rulesets_dir)  # no cache_dir
    with pytest.raises(RulesetError, match="no cache_dir"):
        store.load("vt")


def test_remote_stub_network_failure_keeps_revision_stable(tmp_path, monkeypatch):
    """Network outage during revision() must not 500 the tile route — fall back
    to hashing the stub alone so tiles still serve (from their pre-existing cache)."""
    cache_dir = tmp_path / "cache"
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    _write_ruleset(rulesets_dir / "vt.json", {"$remote": "https://example.com/r.json"})

    import httpx as _httpx

    def raising_get(*args, **kwargs):
        raise _httpx.ConnectError("offline")

    monkeypatch.setattr(_httpx, "get", raising_get)
    store = RulesetStore(rulesets_dir, cache_dir=cache_dir)
    rev = store.revision("vt")
    assert isinstance(rev, str) and len(rev) == 16


def test_remote_stub_unsupported_scheme_raises(tmp_path):
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    _write_ruleset(rulesets_dir / "vt.json", {"$remote": "ftp://example.com/r.json"})
    store = RulesetStore(rulesets_dir, cache_dir=tmp_path / "cache")
    with pytest.raises(RulesetError, match="Unsupported"):
        store.load("vt")


def test_extract_remote_pointer_ignores_non_string():
    from georender_service.rules import _extract_remote_pointer

    assert _extract_remote_pointer(json.dumps({"$remote": 42}).encode()) is None
    assert _extract_remote_pointer(json.dumps({"$remote": ""}).encode()) is None
    assert _extract_remote_pointer(b"not json") is None
    assert _extract_remote_pointer(json.dumps([]).encode()) is None


def test_register_inline_takes_precedence_over_disk(tmp_path):
    rulesets_dir = tmp_path / "rulesets"
    rulesets_dir.mkdir()
    on_disk = {
        "background": "#000000ff",
        "rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}],
    }
    _write_ruleset(rulesets_dir / "vt.json", on_disk)

    inline = {
        "background": "#ffffffff",
        "rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}],
    }
    store = RulesetStore(rulesets_dir)
    store.register_inline("vt", inline)
    assert store.load("vt")["background"] == "#ffffffff"


def test_register_inline_lists_in_names(tmp_path):
    store = RulesetStore(tmp_path)
    store.register_inline(
        "adhoc",
        {"rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}]},
    )
    assert "adhoc" in store.list_names()


def test_register_inline_revision_is_content_addressed(tmp_path):
    store = RulesetStore(tmp_path)
    body = {"rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}]}
    store.register_inline("a", body)
    rev1 = store.revision("a")
    store.register_inline(
        "a",
        {"background": "#ff0000", "rules": body["rules"]},
    )
    rev2 = store.revision("a")
    assert rev1 != rev2


def test_register_inline_rejects_non_dict(tmp_path):
    store = RulesetStore(tmp_path)
    with pytest.raises(RulesetError, match="JSON object"):
        store.register_inline("bad", [1, 2, 3])  # type: ignore[arg-type]


def test_clear_inline_removes_entries(tmp_path):
    store = RulesetStore(tmp_path)
    store.register_inline(
        "x",
        {"rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}]},
    )
    store.clear_inline()
    with pytest.raises(RulesetError, match="not found"):
        store.load("x")


def test_wms_rule_validates_without_geometry(tmp_path):
    """WMS rules paint the viewport once; geometry is not required."""
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "w.json",
        {
            "rules": [
                {
                    "name": "aerial",
                    "z_index": 0,
                    "symbolizer": {
                        "type": "wms",
                        "url": "https://example.com/wms",
                        "layers": "RER_1976",
                    },
                }
            ]
        },
    )
    data = store.load("w")
    assert data["rules"][0]["symbolizer"]["type"] == "wms"


def test_wms_rule_with_invalid_geometry_still_rejected(tmp_path):
    """If a WMS rule does declare a geometry list, bad entries still fail."""
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "w.json",
        {
            "rules": [
                {
                    "geometry": ["Banana"],
                    "symbolizer": {
                        "type": "wms",
                        "url": "https://example.com/wms",
                        "layers": "X",
                    },
                }
            ]
        },
    )
    with pytest.raises(RulesetError, match="geometry"):
        store.load("w")
