"""Vérification complète de l'installation, en une commande.

    python -m smartsupply.verify

Chaque point affiche OK / ATTENTION / ERREUR, et la correction à faire si besoin.
Ne modifie rien : lecture seule.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text

from smartsupply.db.connection import PROJECT_ROOT, get_engine

load_dotenv(PROJECT_ROOT / ".env")

EXPECTED_VIEWS = {
    "v_daily_sales", "v_daily_kpis", "v_product_performance", "v_stock_status",
    "v_supplier_reliability", "v_incoming_receipts", "v_promotions",
    "v_forecasts", "v_forecast_accuracy", "v_model_runs",
    "v_clients", "v_clients_monthly", "v_pipeline_health",
    "v_monitoring_daily", "v_drift_metrics", "v_pipeline_runs",
}
EXPECTED_ML_TABLES = {"feature_snapshots", "training_datasets", "model_runs", "forecasts",
                      "drift_metrics", "monitoring_daily"}
DEPLOYMENT = "cycle-nocturne/chaque-nuit"
OLD_DEPLOYMENT = "simulation-journaliere/toutes-les-5h"

results: list[tuple[str, str, str, str]] = []   # (statut, sujet, détail, correction)


def report(status: str, subject: str, detail: str = "", fix: str = "") -> None:
    results.append((status, subject, detail, fix))


def ok(subject, detail=""):
    report("OK", subject, detail)


def warn(subject, detail, fix=""):
    report("ATTENTION", subject, detail, fix)


def error(subject, detail, fix=""):
    report("ERREUR", subject, detail, fix)


# -----------------------------------------------------------------------------
def check_database(conn) -> None:
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    q = lambda sql, **p: conn.execute(text(sql), p).scalar()   # noqa: E731

    # Structure
    views = {r[0] for r in conn.execute(text(
        "SELECT table_name FROM information_schema.views WHERE table_schema = 'analytics'"))}
    missing = EXPECTED_VIEWS - views
    if missing:
        error("Vues Power BI", f"manquantes : {', '.join(sorted(missing))}",
              "docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/analytics_views.sql")
    else:
        ok("Vues Power BI", f"{len(EXPECTED_VIEWS)} vues présentes")
    tables = {r[0] for r in conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'ml'"))}
    missing = EXPECTED_ML_TABLES - tables
    if missing:
        error("Tables ML", f"manquantes : {', '.join(sorted(missing))}",
              "docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/schema.sql")
        return
    ok("Tables ML", f"{len(EXPECTED_ML_TABLES)} tables présentes")

    # Calendrier réel
    first, last = conn.execute(text("SELECT min(simulated_date), max(simulated_date) FROM raw.simulation_runs")).one()
    if last is None:
        error("Simulation", "aucune journée simulée", "python -m smartsupply.generator.bootstrap")
        return
    n_days = q("SELECT count(*) FROM raw.simulation_runs")
    holes = (last - first).days + 1 - n_days
    if holes:
        error("Simulation", f"{holes} journée(s) manquante(s) entre {first} et {last}")
    if last > yesterday:
        error("Calendrier", f"données jusqu'au {last}, après hier ({yesterday})",
              "reset_simulation.sql puis bootstrap (voir model_card)")
    elif last < yesterday:
        warn("Calendrier", f"données jusqu'au {last}, hier = {yesterday} : {(yesterday - last).days} jour(s) de retard",
             "le pipeline rattrape au prochain run (ou : python -m smartsupply.generator.once)")
    else:
        ok("Calendrier", f"{first} -> {last} ({n_days} jours, aucun dans le futur)")
    future_orders = q("SELECT count(*) FROM raw.orders WHERE (ordered_at AT TIME ZONE 'UTC')::date > :d", d=yesterday)
    if future_orders:
        error("Commandes", f"{future_orders} commande(s) datée(s) après hier")
    else:
        n_orders = f"{q('SELECT count(*) FROM raw.orders'):,}".replace(",", " ")
        ok("Commandes", f"{n_orders} commandes, aucune dans le futur")

    # Qualité
    try:
        from smartsupply.quality.checks import run_quality_checks
        run_quality_checks(conn.engine)
        ok("Contrôles qualité", "les 6 contrôles passent")
    except Exception as exc:                                        # noqa: BLE001
        error("Contrôles qualité", str(exc))

    # Features
    fs_max = q("SELECT max(as_of_date) FROM ml.feature_snapshots")
    n_products = q("SELECT count(*) FROM raw.products")
    if fs_max != last:
        warn("Dataset ML", f"features jusqu'au {fs_max}, simulation jusqu'au {last}",
             "python -m smartsupply.features.build")
    else:
        n_latest = q("SELECT count(*) FROM ml.feature_snapshots WHERE as_of_date = :d", d=fs_max)
        (ok if n_latest == n_products else error)(
            "Dataset ML", f"à jour ({fs_max}), {n_latest}/{n_products} produits")

    # Modèle en service
    champion = conn.execute(text(
        "SELECT run_id, data_until, wape, baseline_wape, bias FROM ml.model_runs "
        "WHERE promoted ORDER BY trained_at DESC LIMIT 1")).first()
    if champion is None:
        error("Modèle en service", "aucun", "python -m smartsupply.models.nightly")
    else:
        labeled_max = q("SELECT max(as_of_date) FROM ml.feature_snapshots WHERE target_demand_next_7d IS NOT NULL")
        last_run = q("SELECT max(data_until) FROM ml.model_runs")
        detail = (f"{champion.run_id} : WAPE {champion.wape:.1%} (naïve {champion.baseline_wape:.1%}), "
                  f"biais {champion.bias:+.1%}")
        if champion.wape >= champion.baseline_wape:
            error("Modèle en service", detail + " — ne bat pas la méthode naïve")
        elif last_run != labeled_max:
            warn("Modèle en service", detail + f" — dernier entraînement sur données au {last_run}, "
                 f"disponibles au {labeled_max}", "python -m smartsupply.models.nightly")
        else:
            ok("Modèle en service", detail)
        refused = q("SELECT count(*) FROM ml.model_runs WHERE NOT promoted")
        if refused:
            warn("Entraînements refusés", f"{refused} (voir analytics.v_model_runs)")

    # Prévision
    fc_max = q("SELECT max(as_of_date) FROM ml.forecasts")
    if fc_max is None:
        error("Prévision", "aucune", "python -m smartsupply.models.nightly")
    elif fc_max != fs_max:
        warn("Prévision", f"dernière prévision du {fc_max}, données au {fs_max}",
             "python -m smartsupply.models.nightly")
    else:
        n = q("SELECT count(*) FROM ml.forecasts WHERE as_of_date = :d", d=fc_max)
        alerts = q("SELECT count(*) FROM analytics.v_forecasts WHERE is_latest AND forecast_alert = 'rupture probable'")
        (ok if n == n_products else error)(
            "Prévision", f"du {fc_max} pour {fc_max + timedelta(days=1)} -> {fc_max + timedelta(days=7)}, "
            f"{n}/{n_products} produits, {alerts} rupture(s) probable(s)")

    # Surveillance du modèle (dérive + erreur réelle)
    if q("SELECT to_regclass('ml.monitoring_daily')") is None:
        error("Surveillance", "tables absentes",
              "docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/schema.sql")
    else:
        d = conn.execute(text(
            "SELECT as_of_date, max_psi, top_feature, n_watch, n_strong FROM ml.monitoring_daily "
            "WHERE max_psi IS NOT NULL ORDER BY as_of_date DESC LIMIT 1")).first()
        r = conn.execute(text(
            "SELECT count(*), avg(real_wape) FILTER (WHERE as_of_date > (SELECT max(as_of_date) - 28 "
            "FROM ml.monitoring_daily WHERE real_wape IS NOT NULL)) FROM ml.monitoring_daily "
            "WHERE real_wape IS NOT NULL")).one()
        real = f", erreur réelle 4 dern. sem. {r[1]:.1%} ({r[0]} prévisions vérifiées)" if r[0] else \
            ", erreur réelle : pas encore de prévision vieille de 7 jours"
        if d is None:
            warn("Surveillance", "pas encore calculée", "python -m smartsupply.models.monitoring")
        elif d.as_of_date != fs_max:
            warn("Surveillance", f"dernière dérive calculée le {d.as_of_date}, données au {fs_max}",
                 "python -m smartsupply.models.monitoring")
        elif d.n_strong:
            warn("Surveillance", f"dérive forte sur {d.n_strong} variable(s), dont {d.top_feature} "
                 f"(PSI {d.max_psi:.2f}){real}", "voir MLflow ou ml.drift_metrics")
        else:
            ok("Surveillance", f"dérive {'à surveiller' if d.n_watch else 'stable'} "
               f"(PSI max {d.max_psi:.2f}, {d.top_feature}){real}")

    # Cycle automatique (même règle que la carte Power BI et l'alerte Windows)
    has_journal = q("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'ops' AND table_name = 'pipeline_runs'")
    if not has_journal:
        error("Cycle automatique", "journal ops.pipeline_runs absent",
              "docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/schema.sql")
    elif "v_pipeline_health" in views:
        h = conn.execute(text("SELECT health_status, health_message FROM analytics.v_pipeline_health")).one()
        if h.health_status == "ok":
            ok("Cycle automatique", h.health_message)
        elif h.health_status == "aucun passage":
            warn("Cycle automatique", h.health_message,
                 "docker compose build pipeline ; docker compose up -d pipeline")
        else:
            error("Cycle automatique", f"{h.health_status} : {h.health_message}",
                  "voir Prefect : http://127.0.0.1:14200")

    # Compte Power BI
    exists = q("SELECT count(*) FROM pg_roles WHERE rolname = 'powerbi_reader'")
    if not exists:
        error("Compte Power BI", "powerbi_reader absent", "voir database/roles.sql")
    else:
        missing = [v for v in sorted(EXPECTED_VIEWS) if not q(
            "SELECT has_table_privilege('powerbi_reader', :t, 'SELECT')", t=f"analytics.{v}")]
        raw_access = q("SELECT has_schema_privilege('powerbi_reader', 'raw', 'USAGE')")
        if missing:
            error("Compte Power BI", f"pas de lecture sur : {', '.join(missing)}",
                  "GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO powerbi_reader;")
        elif raw_access:
            error("Compte Power BI", "a accès au schéma raw (devrait être interdit)")
        else:
            ok("Compte Power BI", "lecture seule sur analytics, aucun accès à raw")


# -----------------------------------------------------------------------------
def _prefect_post(path: str, body: dict):
    url = os.environ.get("PREFECT_API_URL", "http://127.0.0.1:14200/api").rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def check_prefect() -> None:
    try:
        deployments = _prefect_post("/deployments/filter", {"limit": 50})
        flows = {f["id"]: f["name"] for f in _prefect_post("/flows/filter", {"limit": 50})}
    except Exception as exc:                                        # noqa: BLE001
        error("Prefect", f"serveur injoignable ({exc})", "docker compose up -d prefect")
        return
    names = {f"{flows.get(d['flow_id'], '?')}/{d['name']}": d for d in deployments}
    if OLD_DEPLOYMENT in names:
        warn("Prefect", f"ancienne planification encore présente : {OLD_DEPLOYMENT}",
             f'docker compose exec pipeline prefect deployment delete "{OLD_DEPLOYMENT}"')
    current = names.get(DEPLOYMENT)
    if current is None:
        error("Prefect", f"planification {DEPLOYMENT} absente",
              "docker compose build pipeline ; docker compose up -d pipeline")
        return
    try:
        runs = _prefect_post("/flow_runs/filter", {
            "limit": 1, "sort": "EXPECTED_START_TIME_ASC",
            "flow_runs": {"deployment_id": {"any_": [current["id"]]}, "state": {"type": {"any_": ["SCHEDULED"]}}}})
        nxt = runs[0]["expected_start_time"] if runs else None
    except Exception:                                               # noqa: BLE001
        nxt = None
    if nxt:
        when = datetime.fromisoformat(nxt.replace("Z", "+00:00")).astimezone()
        ok("Prefect", f"{DEPLOYMENT} planifié, prochain run : {when:%d/%m %H:%M} (heure locale)")
    else:
        warn("Prefect", f"{DEPLOYMENT} présent mais aucun run planifié",
             "vérifie que le conteneur pipeline tourne : docker compose ps")


def check_mlflow() -> None:
    from smartsupply.models import tracking

    uri = tracking.tracking_uri()
    if not tracking.server_available(uri):
        error("MLflow", f"serveur injoignable ({uri})", "docker compose up -d mlflow")
        return
    try:
        from mlflow import MlflowClient
        client = MlflowClient(uri)
        experiment = client.get_experiment_by_name(tracking.EXPERIMENT)
        n_runs = len(client.search_runs([experiment.experiment_id], max_results=50000)) if experiment else 0
        with get_engine().connect() as conn:
            n_db = conn.execute(text("SELECT count(*) FROM ml.model_runs")).scalar()
            champion = conn.execute(text(
                "SELECT run_id FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC LIMIT 1")).scalar()
        try:
            version = client.get_model_version_by_alias(tracking.REGISTERED_MODEL, tracking.CHAMPION_ALIAS)
            alias_run = client.get_run(version.run_id).data.tags.get(tracking.RUN_ID_TAG)
        except Exception:  # noqa: BLE001  (registre vide ou alias absent)
            version, alias_run = None, None
    except Exception as exc:  # noqa: BLE001
        error("MLflow", f"lecture impossible ({exc})")
        return
    if n_runs < n_db:
        warn("MLflow", f"{n_runs}/{n_db} entraînements recopiés", "python -m smartsupply.models.tracking")
    elif champion and alias_run != champion:
        warn("MLflow", f"alias champion sur {alias_run}, modèle en service {champion}",
             "python -m smartsupply.models.tracking")
    else:
        ok("MLflow", f"{n_runs} entraînement(s), champion = version {version.version if version else '-'}"
                     f" ({champion}) — {uri}")


def check_prometheus() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    base = f"http://127.0.0.1:{os.getenv('PROMETHEUS_PORT', '19090')}/api/v1"
    try:
        with urllib.request.urlopen(f"{base}/targets", timeout=5) as resp:
            targets = json.loads(resp.read())["data"]["activeTargets"]
        with urllib.request.urlopen(f"{base}/alerts", timeout=5) as resp:
            alerts = [a for a in json.loads(resp.read())["data"]["alerts"] if a["state"] == "firing"]
        query = urllib.parse.quote("time() - smartsupply_pipeline_last_run_timestamp_seconds")
        with urllib.request.urlopen(f"{base}/query?query={query}", timeout=5) as resp:
            pushed = json.loads(resp.read())["data"]["result"]
    except Exception as exc:  # noqa: BLE001
        error("Prometheus", f"injoignable ({exc})", "docker compose up -d prometheus")
        return
    down = sorted(t["labels"]["job"] for t in targets if t["health"] != "up")
    if down:
        warn("Prometheus", f"cibles injoignables : {', '.join(down)} "
             f"({len(targets) - len(down)}/{len(targets)} OK)", "docker compose ps")
    elif not pushed:
        warn("Prometheus", "aucun chiffre du cycle reçu pour l'instant",
             "attendre le prochain passage, ou : docker compose restart pipeline")
    else:
        ok("Prometheus", f"{len(targets)} cibles relevées, dernier dépôt du cycle il y a "
                         f"{float(pushed[0]['value'][1]) / 60:.0f} min")
    if alerts:
        names = ", ".join(sorted({a["labels"]["alertname"] for a in alerts}))
        warn("Alertes", f"{len(alerts)} en cours : {names}", "voir http://127.0.0.1:19090/alerts")
    else:
        ok("Alertes", "aucune alerte en cours")


def check_smtp() -> tuple[str, str]:
    """Se connecte au serveur d'envoi avec le compte de .env, SANS envoyer de message."""
    import smtplib

    host, _, port = os.getenv("SMTP_HOST", "smtp.gmail.com:587").partition(":")
    user, password = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
    to = os.getenv("ALERT_EMAIL_TO", "")
    if not user or not password or "example" in user or user.startswith("ton.adresse"):
        return "attention", "en marche, mais l'envoi d'e-mails n'est pas configuré dans .env"
    try:
        with smtplib.SMTP(host, int(port or 587), timeout=10) as smtp:
            smtp.starttls()
            smtp.login(user, password)
    except smtplib.SMTPAuthenticationError:
        return "attention", (f"la messagerie refuse le compte {user} : vérifier SMTP_PASSWORD "
                             "(mot de passe d'application, sans espaces)")
    except Exception as exc:  # noqa: BLE001
        return "attention", f"serveur d'envoi {host} injoignable ({type(exc).__name__})"
    return "ok", f"en marche, alertes envoyées par e-mail à {to or user}"


def check_grafana_alertmanager() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    # Alertmanager : en marche, et la messagerie accepte-t-elle le compte d'envoi ?
    am = f"http://127.0.0.1:{os.getenv('ALERTMANAGER_PORT', '19093')}"
    try:
        with urllib.request.urlopen(f"{am}/-/healthy", timeout=5):
            pass
    except Exception as exc:  # noqa: BLE001
        error("Alertmanager", f"injoignable ({exc})", "docker compose up -d alertmanager")
    else:
        status, detail = check_smtp()
        if status == "ok":
            ok("Alertmanager", detail)
        else:
            warn("Alertmanager", detail, "compléter SMTP_* et ALERT_EMAIL_TO dans .env (voir .env.example), "
                                         "puis : docker compose up -d alertmanager")
    # Grafana : en marche, et les sources de données répondent-elles ?
    gf = f"http://127.0.0.1:{os.getenv('GRAFANA_PORT', '19300')}"
    try:
        with urllib.request.urlopen(f"{gf}/api/health", timeout=5) as resp:
            version = json.loads(resp.read()).get("version", "?")
    except Exception as exc:  # noqa: BLE001
        error("Grafana", f"injoignable ({exc})", "docker compose up -d grafana")
        return
    if not os.getenv("GRAFANA_DB_PASSWORD"):
        warn("Grafana", f"v{version} en marche, mais GRAFANA_DB_PASSWORD absent de .env : "
             "les tableaux qui lisent la base resteront vides",
             "voir .env.example et database/roles_grafana.sql")
        return
    try:
        import psycopg
        with psycopg.connect(host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                             port=int(os.getenv("POSTGRES_PORT", "15432")),
                             dbname=os.environ["POSTGRES_DB"], user="grafana_reader",
                             password=os.environ["GRAFANA_DB_PASSWORD"], connect_timeout=5) as conn:
            conn.execute("SELECT 1 FROM analytics.v_pipeline_health").fetchone()
        ok("Grafana", f"v{version} en marche, compte grafana_reader OK — {gf}")
    except Exception as exc:  # noqa: BLE001
        error("Grafana", f"le compte grafana_reader ne peut pas lire la base ({str(exc).strip()[:120]})",
              "docker compose exec postgres psql -U smartsupply -d smartsupply "
              "-v grafana_password='<GRAFANA_DB_PASSWORD>' -f /database/roles_grafana.sql")


def check_api() -> None:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    base = f"http://127.0.0.1:{os.getenv('API_PORT', '18800')}"
    try:
        with urllib.request.urlopen(f"{base}/sante", timeout=15) as resp:
            s = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        error("API de prévision", f"injoignable ({exc})", "docker compose up -d api")
        return
    m = s.get("modele")
    if s.get("statut") != "ok" or not m:
        error("API de prévision", f"dégradée : {s.get('erreur')}", "docker compose logs --tail 30 api")
    elif m["source"] != "mlflow":
        warn("API de prévision", f"sert le modèle {m['run_id']} depuis la base (MLflow indisponible)",
             "docker compose up -d mlflow ; l'API repasse sur MLflow en 5 min")
    else:
        ok("API de prévision", f"champion MLflow version {m['version_mlflow']} ({m['run_id']}) — {base}/docs")


def check_docker() -> None:
    try:
        out = subprocess.run(["docker", "compose", "ps", "--format", "json"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        warn("Docker", "commande docker indisponible ici (vérification ignorée)")
        return
    if out.returncode != 0:
        error("Docker", out.stderr.strip()[:200], "démarre Docker Desktop")
        return
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip().startswith("{")]
    if not lines and out.stdout.strip().startswith("["):
        lines = json.loads(out.stdout)
    states = {c.get("Service"): c.get("State") for c in lines}
    for service in ("postgres", "prefect", "mlflow", "pipeline", "api", "pushgateway",
                    "postgres-exporter", "prometheus", "alertmanager", "grafana"):
        state = states.get(service)
        if state == "running":
            ok(f"Conteneur {service}", "en marche")
        else:
            error(f"Conteneur {service}", f"état : {state or 'absent'}", f"docker compose up -d {service}")


# -----------------------------------------------------------------------------
def main() -> None:
    print(f"Vérification SmartSupply — {datetime.now():%d/%m/%Y %H:%M}\n")
    check_docker()
    try:
        with get_engine().connect() as conn:
            ok("PostgreSQL", "connexion réussie")
            check_database(conn)
    except Exception as exc:                                        # noqa: BLE001
        error("PostgreSQL", f"connexion impossible ({getattr(exc, 'orig', exc)})",
              "docker compose up -d --force-recreate postgres")
    check_prefect()
    check_mlflow()
    check_prometheus()
    check_grafana_alertmanager()
    check_api()

    width = max(len(s) for _, s, _, _ in results)
    for status, subject, detail, fix in results:
        mark = {"OK": "[OK]       ", "ATTENTION": "[ATTENTION]", "ERREUR": "[ERREUR]   "}[status]
        print(f"{mark} {subject:<{width}}  {detail}")
        if fix:
            print(f"{'':12} {'':<{width}}  -> {fix}")
    counts = {s: sum(1 for r in results if r[0] == s) for s in ("OK", "ATTENTION", "ERREUR")}
    print(f"\n{counts['OK']} OK, {counts['ATTENTION']} attention, {counts['ERREUR']} erreur(s)")
    raise SystemExit(1 if counts["ERREUR"] else 0)


if __name__ == "__main__":
    main()
