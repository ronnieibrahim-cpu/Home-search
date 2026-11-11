"""The committed config files must always load, and the scaffold's guards must hold."""

from pathlib import Path

import pytest
import yaml

from home_search import cli, config


def test_search_config_loads():
    cfg = config.load_search()
    assert cfg.purchase.price_min == 800_000
    assert cfg.purchase.price_max == 1_200_000
    assert {a.id for a in cfg.areas} == {"bellaire", "memorial"}
    assert "Memorial High School" in cfg.area("memorial").target_schools.high
    assert "Bellaire High School" in cfg.area("bellaire").target_schools.high


def test_assumptions_load_and_cover_every_area():
    cfg, a = config.load_search(), config.load_assumptions()
    for area in cfg.areas:
        assert area.id in a.property_tax.rate_by_area
    assert a.horizon_years >= 10


def test_weights_must_sum_to_one():
    raw = yaml.safe_load((config.CONFIG_DIR / "search.yaml").read_text())
    raw["scoring"]["weights"]["schools"] += 0.1
    with pytest.raises(ValueError):
        config.SearchConfig.model_validate(raw)


def test_example_profile_is_valid_but_blank():
    p = config.Profile.model_validate(
        yaml.safe_load((config.CONFIG_DIR / "profile.example.yaml").read_text())
    )
    assert not p.is_filled_in


def test_profile_from_env_wins(monkeypatch):
    monkeypatch.setenv("PROFILE_YAML", "income: {gross_monthly_for_lender: 1}\ncash_for_purchase: {balance: 1}\nprice_grid: []")
    assert config.load_profile().is_filled_in


def test_real_profile_is_gitignored():
    ignored = (Path(config.ROOT) / ".gitignore").read_text()
    assert "config/profile.yaml" in ignored and "out/" in ignored


def test_doctor_runs(capsys):
    assert cli.doctor() == 0
    assert "search.yaml       ok" in capsys.readouterr().out
