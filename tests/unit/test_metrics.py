"""Chiffres publiés pour Prometheus (sans Pushgateway ni base)."""
from datetime import date

from prometheus_client import generate_latest

from smartsupply import metrics

STATE = {
    "data_last_day": date(2026, 11, 20),
    "model": {"wape": 0.124, "baseline_wape": 0.191, "bias": 0.004, "trained_at": 1.79e9},
    "model_runs": {"retenu": 1},
    "drift": {"max_psi": 0.11, "n_watch": 1, "n_strong": 0, "psi": {"cv_28": 0.03}},
    "real": None, "forecast_units": 4658, "stockouts": 0,
}


def text(registry):
    return generate_latest(registry).decode()


def test_reussite_publie_tout():
    out = text(metrics.build_registry(True, 1000.0, {"prevision": 0.1}, 0, STATE))
    assert "smartsupply_pipeline_last_run_success 1.0" in out
    assert "smartsupply_pipeline_last_success_timestamp_seconds 1000.0" in out
    assert 'smartsupply_pipeline_step_duration_seconds{step="prevision"} 0.1' in out
    assert "smartsupply_model_wape 0.124" in out
    assert 'smartsupply_drift_features{level="forte"} 0.0' in out
    assert 'smartsupply_drift_psi{feature="cv_28"} 0.03' in out
    assert "smartsupply_data_last_day_timestamp_seconds 1.7951328e+09" in out   # 20/11/2026 minuit UTC
    assert "smartsupply_real_wape" not in out                                   # pas encore connue


def test_echec_ne_publie_que_le_resultat():
    # Les autres chiffres du passage précédent restent dans le Pushgateway (pushadd).
    out = text(metrics.build_registry(False, 1000.0, state=STATE))
    assert "smartsupply_pipeline_last_run_success 0.0" in out
    assert "last_success" not in out and "model_wape" not in out


def test_pushgateway_absent_ne_bloque_pas(monkeypatch):
    monkeypatch.setenv("PUSHGATEWAY_URL", "http://127.0.0.1:9")
    status = metrics.push(metrics.build_registry(True, 1.0), timeout=1)
    assert status.startswith("Pushgateway injoignable")


def test_chronometre():
    timer = metrics.StepTimer()
    with timer("a"):
        pass
    assert set(timer.durations) == {"a"} and timer.durations["a"] >= 0
