# research/tests/test_manifests_dir_delegation.py
"""All root-resolvers honor RESEARCH_INTERVAL — run from research/ (pytest tests/)."""
import importlib


def test_factor_extended_resolver_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "30m")
    import factor_extended
    importlib.reload(factor_extended)
    assert factor_extended.resolve_manifests_dir().name == "30m"


def test_factor_regime_resolver_namespaced(monkeypatch):
    monkeypatch.setenv("RESEARCH_INTERVAL", "15m")
    import factor_regime
    importlib.reload(factor_regime)
    assert factor_regime._resolve_manifests_dir().name == "15m"


def test_resolvers_root_when_unset(monkeypatch):
    monkeypatch.delenv("RESEARCH_INTERVAL", raising=False)
    import factor_extended
    importlib.reload(factor_extended)
    assert factor_extended.resolve_manifests_dir().name == "manifests"


def test_stage2_5_regime_main_uses_active_manifests_dir():
    # regime_<sym>.json must namespace by interval like the other resolvers.
    # main() resolves the dir inline, so lock the wiring at the source level.
    import inspect

    from pipeline import stage2_5_regime

    src = inspect.getsource(stage2_5_regime.main)
    assert "active_manifests_dir()" in src
