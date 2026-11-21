"""État du cycle automatique, et alerte Windows si quelque chose ne va pas.

    python -m smartsupply.health            # affiche l'état
    python -m smartsupply.health --notify   # + fenêtre d'alerte Windows SI problème

La règle est celle de la vue analytics.v_pipeline_health (une seule source de vérité,
la même que la carte « État du cycle » dans Power BI) :
    ok          les données d'hier et leur prévision sont là
    en retard   il manque la journée d'hier ou sa prévision
    échec       le dernier passage du cycle a planté
    aucun passage
Si la base ne répond pas, c'est aussi une alerte (Docker Desktop arrêté, PC sorti de veille...).

Une tâche planifiée Windows lance la version --notify à l'ouverture de session et
chaque matin (voir scripts/install_alert.ps1). Quand tout va bien : aucune fenêtre.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from smartsupply.db.connection import get_engine

TITLE = "SmartSupply - cycle automatique"
FIX_HINTS = {
    "échec": "Voir le détail dans Prefect : http://127.0.0.1:14200",
    "en retard": "Le PC était peut-être éteint ou en veille : le rattrapage passe au plus tard 3 h après "
                 "son réveil. Si ça dure : docker compose up -d",
    "aucun passage": "Démarrer le cycle : docker compose up -d",
    "base injoignable": "Lancer Docker Desktop, puis : docker compose up -d "
                        "(après une mise en veille : docker compose up -d --force-recreate postgres)",
}


def read_health() -> dict:
    """La ligne de analytics.v_pipeline_health, ou un état « base injoignable »."""
    try:
        with get_engine().connect() as conn:
            row = conn.execute(text("SELECT * FROM analytics.v_pipeline_health")).mappings().one()
        return dict(row)
    except Exception as exc:  # noqa: BLE001  (base arrêtée, vue absente, mot de passe...)
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return {"health_status": "base injoignable", "health_message": first_line[:300]}


def format_report(h: dict) -> str:
    lines = [f"État : {h['health_status'].upper()}", h["health_message"]]
    if h.get("last_run_at") is not None:
        lines.append(f"Dernier passage : {h['last_run_at'].astimezone():%d/%m/%Y %H:%M} ({h['last_run_status']})")
    if h.get("hours_since_success") is not None:
        lines.append(f"Dernière réussite il y a {h['hours_since_success']} h")
    hint = FIX_HINTS.get(h["health_status"])
    if hint:
        lines += ["", f"À faire : {hint}"]
    return "\n".join(lines)


def notify(message: str) -> bool:
    """Fenêtre d'alerte Windows (sans dépendance). Ailleurs : rien, le texte est déjà affiché."""
    if sys.platform != "win32":
        return False
    import ctypes
    MB_ICONWARNING, MB_TOPMOST, MB_SETFOREGROUND = 0x30, 0x40000, 0x10000
    ctypes.windll.user32.MessageBoxW(None, message, TITLE, MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="État du cycle automatique SmartSupply.")
    parser.add_argument("--notify", action="store_true", help="fenêtre d'alerte Windows en cas de problème")
    args = parser.parse_args()

    health = read_health()
    report = format_report(health)
    print(report)
    problem = health["health_status"] != "ok"
    if problem and args.notify:
        notify(report)
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())
