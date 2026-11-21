"""Contrôle des sauvegardes (smartsupply.verify.check_backups), sans Docker ni base."""
import os
import time

from smartsupply import verify


def _run(folder, now=None):
    verify.results.clear()
    verify.check_backups(folder, now=now)
    return verify.results[-1]


def test_aucune_sauvegarde(tmp_path):
    status, subject, detail, fix = _run(tmp_path / "absent")
    assert status == "ERREUR" and "aucune sauvegarde" in detail and "--maintenant" in fix


def test_sauvegarde_recente_puis_ancienne(tmp_path):
    old = tmp_path / "smartsupply_20261101_023000.dump"
    new = tmp_path / "smartsupply_20261120_023000.dump"
    for f in (old, new):
        f.write_bytes(b"x" * 5000)
    now = time.time()
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    os.utime(new, (now - 2 * 86400, now - 2 * 86400))
    status, _, detail, _ = _run(tmp_path, now=now)
    assert status == "OK" and "2 sauvegarde(s)" in detail and new.name in detail
    status, *_ = _run(tmp_path, now=now + 7 * 86400)                 # 9 jours plus tard
    assert status == "ATTENTION"


def test_sauvegarde_vide(tmp_path):
    (tmp_path / "smartsupply_20261120_023000.dump").write_bytes(b"")
    assert _run(tmp_path)[0] == "ERREUR"
