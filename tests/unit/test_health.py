"""Alerte du cycle automatique : texte affiché et décision d'alerter (sans base)."""
from datetime import datetime, timezone

import pytest

from smartsupply import health


def make(status, message="détail"):
    return {"health_status": status, "health_message": message, "last_run_at": datetime(2026, 11, 21, 1, 41, tzinfo=timezone.utc),
            "last_run_status": "ok", "hours_since_success": 12.0}


def test_rapport_ok_sans_consigne():
    text = health.format_report(make("ok", "Tout est à jour"))
    assert "OK" in text and "Tout est à jour" in text and "À faire" not in text


@pytest.mark.parametrize("status", ["échec", "en retard", "aucun passage", "base injoignable"])
def test_rapport_probleme_avec_consigne(status):
    assert "À faire" in health.format_report(make(status))


def test_base_injoignable_est_une_alerte(monkeypatch):
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(health, "get_engine", boom)
    h = health.read_health()
    assert h["health_status"] == "base injoignable"
    assert "connection refused" in h["health_message"]
    assert "À faire" in health.format_report(h)      # pas de clés manquantes


@pytest.mark.parametrize("status, code, alerte", [("ok", 0, False), ("en retard", 1, True), ("échec", 1, True)])
def test_fenetre_seulement_en_cas_de_probleme(monkeypatch, status, code, alerte):
    shown = []
    monkeypatch.setattr(health, "read_health", lambda: make(status))
    monkeypatch.setattr(health, "notify", lambda msg: shown.append(msg) or True)
    monkeypatch.setattr("sys.argv", ["health", "--notify"])
    assert health.main() == code
    assert bool(shown) is alerte
