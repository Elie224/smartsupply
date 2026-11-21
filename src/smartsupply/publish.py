"""Publication des résultats pour le site public (application Streamlit).

Chaque nuit, après le cycle, les résultats SANS AUCUN SECRET sont envoyés sur GitHub,
dans la branche `donnees-publiques` du dépôt (remplacée à chaque fois : elle ne
grossit pas) :

    etat.json                       état du cycle, modèle en service, date des données
    previsions.csv                  prévisions du jour par produit (+ stock projeté, alerte)
    ventes_mensuelles.csv           CA, marge, commandes, taux de service par mois
    modeles.csv                     historique des entraînements (retenus / refusés)
    precision.csv                   erreur réelle des prévisions passées
    surveillance.csv                dérive et erreur réelle, jour par jour
    derive.csv                      dérive de chaque variable (dernier jour)
    produits.csv                    catalogue
    modele.joblib                   modèle champion (pour la simulation)
    features_derniere_journee.parquet  données d'entrée du modèle (dernier jour)

Configuration (.env) :
    GITHUB_REPO   = utilisateur/depot
    GITHUB_TOKEN  = jeton « fine-grained », accès Contents en lecture/écriture à CE dépôt seulement

    python -m smartsupply.publish            # publie si de nouveaux résultats existent
    python -m smartsupply.publish --force    # publie dans tous les cas
    python -m smartsupply.publish --dossier sortie_locale   # écrit les fichiers en local (test)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import PROJECT_ROOT, get_engine
from smartsupply.features.build import ALL_COLUMNS

BRANCH = "donnees-publiques"
API = "https://api.github.com"


# -----------------------------------------------------------------------------
# 1) Construire les fichiers publics (lecture seule dans la base)
# -----------------------------------------------------------------------------
def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):          # nombres numpy
        return value.item()
    return str(value)


def build_bundle(engine: Engine) -> dict[str, bytes]:
    q = lambda sql: pd.read_sql(text(sql), conn)  # noqa: E731
    with engine.connect() as conn:
        health = q("SELECT * FROM analytics.v_pipeline_health").iloc[0].to_dict()
        forecasts = q("""
            SELECT as_of_date, horizon_start, horizon_end, product_id, sku, product_name, category,
                   predicted_qty, predicted_revenue_eur, stock_as_of, incoming_qty,
                   projected_stock_end, forecast_alert, model_run_id
            FROM analytics.v_forecasts WHERE is_latest ORDER BY projected_stock_end, product_id""")
        monthly = q("""
            SELECT date_trunc('month', sales_date)::date AS mois,
                   sum(revenue_eur) AS ca_eur, sum(gross_margin_eur) AS marge_eur,
                   sum(n_orders) AS commandes,
                   round(100.0 * sum(fulfilled_qty) / nullif(sum(requested_qty), 0), 2) AS taux_service_pct,
                   count(*) AS jours
            FROM analytics.v_daily_kpis GROUP BY 1 ORDER BY 1""")
        runs = q("SELECT * FROM analytics.v_model_runs ORDER BY trained_at")
        accuracy = q("SELECT * FROM analytics.v_forecast_accuracy ORDER BY as_of_date")
        monitoring = q("SELECT * FROM analytics.v_monitoring_daily ORDER BY as_of_date")
        drift = q("""SELECT as_of_date, feature, psi, level FROM analytics.v_drift_metrics
                     WHERE as_of_date = (SELECT max(as_of_date) FROM analytics.v_drift_metrics)
                     ORDER BY psi DESC""")
        products = q("SELECT product_id, sku, name, category, unit_price FROM raw.products ORDER BY product_id")
        totals = q("""SELECT (SELECT count(*) FROM raw.orders) AS commandes,
                             (SELECT count(*) FROM raw.clients) AS clients,
                             (SELECT count(*) FROM raw.simulation_runs) AS jours_simules,
                             (SELECT min(simulated_date) FROM raw.simulation_runs) AS debut""").iloc[0].to_dict()
        champion = conn.execute(text(
            "SELECT run_id, trained_at, data_until, wape, baseline_wape, bias, model_blob "
            "FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC LIMIT 1")).mappings().first()
        last_day = forecasts["as_of_date"].max() if not forecasts.empty else None
        snapshots = (pd.read_sql(text(f"SELECT {', '.join(ALL_COLUMNS)} FROM ml.feature_snapshots "
                                      "WHERE as_of_date = :d ORDER BY product_id"), conn,
                                 params={"d": last_day}) if last_day else pd.DataFrame())

    etat = {
        "publie_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "donnees_jusqu_au": health.get("last_simulated_day"),
        "prevision_du": last_day,
        "cycle": {"statut": health.get("health_status"), "message": health.get("health_message"),
                  "dernier_passage": health.get("last_run_at")},
        "modele": None if champion is None else {
            "run_id": champion["run_id"], "entraine_le": champion["trained_at"],
            "donnees_jusqu_au": champion["data_until"], "wape": champion["wape"],
            "wape_methode_naive": champion["baseline_wape"], "biais": champion["bias"]},
        "volumes": totals,
        "alertes": forecasts["forecast_alert"].value_counts().to_dict() if not forecasts.empty else {},
        # Versions qui ont produit modele.joblib : le site doit utiliser les mêmes pour le relire.
        "versions": _versions(),
    }
    bundle = {
        "etat.json": json.dumps(etat, default=_json_default, ensure_ascii=False, indent=2).encode("utf-8"),
        "previsions.csv": _csv(forecasts),
        "ventes_mensuelles.csv": _csv(monthly),
        "modeles.csv": _csv(runs),
        "precision.csv": _csv(accuracy),
        "surveillance.csv": _csv(monitoring),
        "derive.csv": _csv(drift),
        "produits.csv": _csv(products),
        "README.md": (
            "# Données publiques SmartSupply AI\n\n"
            "Branche générée automatiquement chaque nuit par le cycle (`smartsupply.publish`), "
            "remplacée à chaque publication. **Données 100 % synthétiques.** "
            "Lue par l'application Streamlit (`app/streamlit_app.py`).\n").encode("utf-8"),
    }
    if champion is not None:
        bundle["modele.joblib"] = bytes(champion["model_blob"])
    if not snapshots.empty:
        buffer = io.BytesIO()
        snapshots.to_parquet(buffer, index=False)
        bundle["features_derniere_journee.parquet"] = buffer.getvalue()
    return bundle


def _key(etat: dict) -> tuple:
    """Ce qui justifie une nouvelle publication : nouvelle prévision, nouveau modèle,
    nouvel état du cycle (l'heure de publication n'en fait pas partie)."""
    return (etat.get("prevision_du"), (etat.get("modele") or {}).get("run_id"),
            (etat.get("cycle") or {}).get("statut"))


# -----------------------------------------------------------------------------
# 2) Envoyer sur GitHub (API Git : un seul commit, branche remplacée)
# -----------------------------------------------------------------------------
class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}/repos/{self.repo}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "smartsupply-publish"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def current_etat(self) -> dict | None:
        try:
            content = self.call("GET", f"/contents/etat.json?ref={BRANCH}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None                         # première publication
            raise
        return json.loads(base64.b64decode(content["content"]))

    def replace_branch(self, files: dict[str, bytes], message: str) -> str:
        tree = []
        for path, data in sorted(files.items()):
            blob = self.call("POST", "/git/blobs", {"content": base64.b64encode(data).decode(),
                                                     "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree_sha = self.call("POST", "/git/trees", {"tree": tree})["sha"]
        commit = self.call("POST", "/git/commits", {"message": message, "tree": tree_sha, "parents": []})
        try:
            self.call("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 422):
                raise
            self.call("POST", "/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
        return commit["sha"]


def config() -> tuple[str | None, str | None]:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("GITHUB_REPO") or None, os.getenv("GITHUB_TOKEN") or None


def publish(engine: Engine | None = None, force: bool = False) -> dict:
    """Publie les résultats. Ne lève jamais d'exception : renvoie un statut lisible."""
    repo, token = config()
    if not repo or not token:
        return {"status": "non configurée (GITHUB_REPO / GITHUB_TOKEN absents de .env)"}
    try:
        bundle = build_bundle(engine or get_engine())
        gh = GitHub(repo, token)
        current = gh.current_etat()
        if not force and current is not None and _key(current) == _key(json.loads(bundle["etat.json"])) \
                and current.get("tailles") == _sizes(bundle):
            return {"status": "déjà à jour", "repo": repo}
        etat = json.loads(bundle["etat.json"])
        etat["tailles"] = _sizes(bundle)
        bundle["etat.json"] = json.dumps(etat, ensure_ascii=False, indent=2).encode("utf-8")
        sha = gh.replace_branch(bundle, f"Données du {etat.get('prevision_du')} "
                                        f"(modèle {(etat.get('modele') or {}).get('run_id')})")
        return {"status": f"publiées ({len(bundle)} fichiers, commit {sha[:7]})", "repo": repo}
    except urllib.error.HTTPError as exc:
        detail = {401: "jeton refusé", 403: "jeton sans droit d'écriture sur ce dépôt",
                  404: "dépôt introuvable (nom ou droits du jeton)",
                  409: "dépôt vide : pousser d'abord la branche main"}.get(exc.code, exc.reason)
        return {"status": f"échec GitHub {exc.code} : {detail}"}
    except Exception as exc:  # noqa: BLE001  (réseau, base...)
        return {"status": f"échec ({type(exc).__name__} : {str(exc)[:150]})"}


def _versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version
    out = {}
    for name in ("lightgbm", "scikit-learn", "numpy", "pandas", "joblib"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            pass
    return out


def _sizes(bundle: dict[str, bytes]) -> dict[str, int]:
    """Taille de chaque fichier (sans etat.json) : change dès qu'un résultat change."""
    return {k: len(v) for k, v in sorted(bundle.items()) if k != "etat.json"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publie les résultats pour le site public.")
    parser.add_argument("--force", action="store_true", help="publier même si rien n'a changé")
    parser.add_argument("--dossier", help="écrire les fichiers dans ce dossier au lieu de GitHub")
    args = parser.parse_args()
    if args.dossier:
        out = Path(args.dossier)
        out.mkdir(parents=True, exist_ok=True)
        for name, data in build_bundle(get_engine()).items():
            (out / name).write_bytes(data)
        print(f"Fichiers écrits dans {out.resolve()}")
        return
    print("Publication :", publish(force=args.force)["status"])


if __name__ == "__main__":
    main()
