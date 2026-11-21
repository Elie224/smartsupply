"""Génère les tableaux de bord Grafana (docker/grafana/dashboards/*.json).

    python scripts/build_grafana_dashboards.py

Trois tableaux, rangés dans le dossier « SmartSupply » de Grafana :
    - Cycle automatique : état, réussites / échecs, durée des étapes, alertes en cours ;
    - Modèle : erreur à l'entraînement et erreur réelle, dérive, historique des modèles ;
    - Infrastructure : services joignables, PostgreSQL, MLflow, mémoire.
Sources : Prometheus (chiffres relevés toutes les 30 s) et PostgreSQL (vues analytics,
compte grafana_reader en lecture seule).
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docker" / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "smartsupply-pg"}

GREEN, ORANGE, RED, BLUE, GREY = "green", "orange", "red", "blue", "text"


# -----------------------------------------------------------------------------
# Briques
# -----------------------------------------------------------------------------
def prom(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref,
            "instant": instant, "range": not instant}


def sql(query: str, fmt: str = "table", ref: str = "A") -> dict:
    return {"datasource": PG, "rawSql": query.strip(), "format": fmt, "rawQuery": True,
            "editorMode": "code", "refId": ref}


def steps(*pairs) -> dict:
    """steps((None, 'green'), (0.1, 'orange'), ...)"""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in pairs]}


def text_map(mapping: dict[str, tuple[str, str]]) -> list:
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
                                          for i, (k, (t, c)) in enumerate(mapping.items())}}]


def panel(kind: str, title: str, pos: tuple[int, int, int, int], targets: list, *,
          unit: str | None = None, thresholds: dict | None = None, mappings: list | None = None,
          description: str = "", decimals: int | None = None, options: dict | None = None,
          custom: dict | None = None, overrides: list | None = None) -> dict:
    x, y, w, h = pos
    defaults: dict = {"custom": custom or {}}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["thresholds"] = thresholds
        defaults["color"] = {"mode": "thresholds"}
    if mappings:
        defaults["mappings"] = mappings
    if decimals is not None:
        defaults["decimals"] = decimals
    ds = targets[0]["datasource"] if targets else None
    return {"type": kind, "title": title, "description": description, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": options or {}}


def stat(title, pos, target, *, text: bool = False, **kw) -> dict:
    # Par défaut, une carte n'affiche que les champs NUMÉRIQUES : pour un texte
    # (« ok », une date...), il faut lui demander tous les champs.
    opts = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text else "",
                              "values": False},
            "colorMode": "background", "graphMode": "none", "textMode": "value",
            "justifyMode": "center", "orientation": "auto"}
    return panel("stat", title, pos, [target], options=opts, **kw)


def timeseries(title, pos, targets, *, draw: str = "line", threshold_lines: bool = False, **kw) -> dict:
    custom = {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 10 if draw == "line" else 80,
              "pointSize": 6, "showPoints": "auto", "spanNulls": True,
              "thresholdsStyle": {"mode": "line+area" if threshold_lines else "off"}}
    opts = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"}}
    return panel("timeseries", title, pos, targets, custom=custom, options=opts, **kw)


def table(title, pos, targets, **kw) -> dict:
    opts = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}
    return panel("table", title, pos, targets, options=opts, **kw)


def row(title: str, y: int) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def dashboard(uid: str, title: str, panels: list, *, time_from: str, description: str,
              refresh: str = "1m") -> dict:
    for i, p in enumerate(panels, start=1):
        p["id"] = i
    return {"uid": uid, "title": title, "description": description, "tags": ["smartsupply"],
            "timezone": "browser", "editable": True, "graphTooltip": 1, "refresh": refresh,
            "schemaVersion": 39, "version": 1, "time": {"from": time_from, "to": "now"},
            "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
            "links": [{"title": "Autres tableaux SmartSupply", "type": "dashboards",
                       "tags": ["smartsupply"], "asDropdown": True}]}


HEALTH_MAP = text_map({"ok": ("OK", GREEN), "en retard": ("EN RETARD", ORANGE),
                       "échec": ("ÉCHEC", RED), "aucun passage": ("AUCUN PASSAGE", ORANGE)})
SUCCESS_MAP = text_map({"1": ("Réussi", GREEN), "0": ("Échec", RED)})
UP_MAP = text_map({"1": ("En marche", GREEN), "0": ("Arrêté", RED)})
LEVEL_MAP = text_map({"stable": ("stable", GREEN), "à surveiller": ("à surveiller", ORANGE),
                      "forte": ("forte", RED)})
PSI_STEPS = steps((None, GREEN), (0.10, ORANGE), (0.25, RED))


def cell_colors(field: str, mappings: list) -> dict:
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": "mappings", "value": mappings},
                           {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}


def renames(**names) -> list:
    return [{"matcher": {"id": "byName", "options": col},
             "properties": [{"id": "displayName", "value": label}]} for col, label in names.items()]


# -----------------------------------------------------------------------------
# 1) Cycle automatique
# -----------------------------------------------------------------------------
def cycle() -> dict:
    p = [
        stat("État du cycle", (0, 0, 6, 5), sql("SELECT health_status FROM analytics.v_pipeline_health"),
             mappings=HEALTH_MAP, text=True,
             description="Même règle que la carte Power BI et l'alerte Windows."),
        stat("Dernière réussite il y a", (6, 0, 6, 5),
             prom("time() - smartsupply_pipeline_last_success_timestamp_seconds", instant=True),
             unit="s", thresholds=steps((None, GREEN), (26 * 3600, RED)),
             description="Rouge au-delà de 26 h (alerte CycleEnRetard)."),
        stat("Dernier passage", (12, 0, 6, 5),
             prom("smartsupply_pipeline_last_run_success", instant=True), mappings=SUCCESS_MAP),
        stat("Données jusqu'au", (18, 0, 6, 5),
             sql("SELECT to_char(last_simulated_day, 'DD/MM/YYYY') AS jour FROM analytics.v_pipeline_health"),
             thresholds=steps((None, BLUE)), text=True),
        table("Alertes en cours", (0, 5, 24, 5),
              [prom('ALERTS{alertstate="firing"}', instant=True)],
              description="Règles dans docker/prometheus/alerts.yml ; envoyées sur Discord par Alertmanager.",
              overrides=renames(alertname="Alerte", severity="Gravité", job="Service")),
        row("Durées", 10),
        timeseries("Durée de chaque étape", (0, 11, 16, 9),
                   [prom("smartsupply_pipeline_step_duration_seconds", "{{step}}")], unit="s",
                   description="Mesurée à chaque passage (2 h 30 + rattrapages toutes les 3 h)."),
        timeseries("Durée totale des passages", (16, 11, 8, 9),
                   [sql("""SELECT finished_at AS time, duration_s AS "Durée (s)"
                           FROM analytics.v_pipeline_runs WHERE $__timeFilter(finished_at)
                           ORDER BY 1""", fmt="time_series")],
                   draw="points", unit="s"),
        row("Journal", 20),
        table("Derniers passages", (0, 21, 24, 10),
              [sql("""SELECT finished_at AS "Fin", status AS "Résultat", duration_s AS "Durée (s)",
                             days_added AS "Journées ajoutées", model_status AS "Modèle",
                             forecast_status AS "Prévision", message AS "Détail"
                      FROM analytics.v_pipeline_runs ORDER BY finished_at DESC LIMIT 50""")],
              overrides=[cell_colors("Résultat", text_map({"ok": ("ok", GREEN), "échec": ("échec", RED)}))]),
    ]
    return dashboard("ss-cycle", "SmartSupply — Cycle automatique", p, time_from="now-7d",
                     description="Le cycle tourne-t-il bien ? Réussites, échecs, durées, alertes.")


# -----------------------------------------------------------------------------
# 2) Modèle
# -----------------------------------------------------------------------------
def modele() -> dict:
    pct = "percentunit"
    p = [
        stat("Erreur du modèle (WAPE, 8 sem.)", (0, 0, 5, 5), prom("smartsupply_model_wape", instant=True),
             unit=pct, decimals=1, thresholds=steps((None, GREEN), (0.15, ORANGE), (0.18, RED)),
             description="Mesurée à l'entraînement, sur les 8 dernières semaines."),
        stat("Méthode naïve (même fenêtre)", (5, 0, 5, 5),
             prom("smartsupply_model_baseline_wape", instant=True), unit=pct, decimals=1,
             thresholds=steps((None, GREY))),
        stat("Erreur réelle (4 dern. sem.)", (10, 0, 5, 5), prom("smartsupply_real_wape_28d", instant=True),
             unit=pct, decimals=1, thresholds=steps((None, GREEN), (0.15, ORANGE), (0.18, RED)),
             description="Prévisions comparées aux ventes réelles, 7 jours après. Vide les premiers jours."),
        stat("Biais", (15, 0, 4, 5), prom("smartsupply_model_bias", instant=True), unit=pct, decimals=1,
             thresholds=steps((None, RED), (-0.05, GREEN), (0.05, RED)),
             description="> 0 : le modèle surestime. Refusé au-delà de ±5 %."),
        stat("Dérive la plus forte (PSI)", (19, 0, 5, 5), prom("smartsupply_drift_max_psi", instant=True),
             decimals=2, thresholds=PSI_STEPS,
             description="< 0,10 stable · 0,10 à 0,25 à surveiller · > 0,25 dérive forte."),
        row("Erreur au fil du temps", 5),
        timeseries("Erreur réelle des prévisions vs erreur à l'entraînement", (0, 6, 16, 9), [
            sql("""SELECT as_of_date::timestamp AS time, real_wape AS "Erreur réelle (par jour de prévision)"
                   FROM analytics.v_monitoring_daily WHERE real_wape IS NOT NULL ORDER BY 1""",
                fmt="time_series", ref="A"),
            sql("""SELECT trained_at AS time, wape_pct / 100.0 AS "Erreur à l'entraînement",
                          baseline_wape_pct / 100.0 AS "Méthode naïve"
                   FROM analytics.v_model_runs ORDER BY 1""", fmt="time_series", ref="B")],
            unit=pct, thresholds=steps((None, "transparent"), (0.18, RED)), threshold_lines=True,
            description="Zone rouge : au-delà de 18 % (alerte ErreurReelleElevee, en moyenne sur 4 semaines)."),
        timeseries("Demande prévue sur 7 jours", (16, 6, 8, 9),
                   [prom("smartsupply_forecast_units_7d", "unités")], unit="none"),
        row("Dérive des données", 15),
        timeseries("Dérive la plus forte, jour par jour", (0, 16, 12, 9),
                   [sql("""SELECT as_of_date::timestamp AS time, max_psi AS "PSI max"
                           FROM analytics.v_monitoring_daily WHERE max_psi IS NOT NULL ORDER BY 1""",
                        fmt="time_series")],
                   thresholds=PSI_STEPS, threshold_lines=True, decimals=2),
        panel("bargauge", "Dérive de chaque variable (aujourd'hui)", (12, 16, 12, 9),
              [prom("sort_desc(smartsupply_drift_psi)", "{{feature}}", instant=True)],
              thresholds=PSI_STEPS, decimals=2,
              options={"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "minVizHeight": 10, "valueMode": "color"},
              custom={}),
        row("Historique des modèles", 25),
        table("Entraînements (retenus et refusés)", (0, 26, 24, 9),
              [sql("""SELECT trained_at AS "Date", run_id AS "Entraînement", wape_pct AS "WAPE %",
                             baseline_wape_pct AS "Naïve %", bias_pct AS "Biais %",
                             CASE WHEN promoted THEN 'retenu' ELSE 'refusé' END AS "Décision",
                             decision AS "Raison", is_current AS "En service"
                      FROM analytics.v_model_runs ORDER BY trained_at DESC""")],
              overrides=[cell_colors("Décision", text_map({"retenu": ("retenu", GREEN),
                                                            "refusé": ("refusé", ORANGE)}))]),
    ]
    return dashboard("ss-modele", "SmartSupply — Modèle", p, time_from="now-90d", refresh="5m",
                     description="Le modèle reste-t-il bon ? Erreur prévue vs réelle, dérive, versions.")


# -----------------------------------------------------------------------------
# 3) Infrastructure
# -----------------------------------------------------------------------------
def infra() -> dict:
    db = 'datname="smartsupply"'
    p = [
        panel("stat", "Services relevés par Prometheus", (0, 0, 24, 4),
              [prom("up", "{{job}}", instant=True)], mappings=UP_MAP,
              thresholds=steps((None, RED), (1, GREEN)),
              options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "colorMode": "background", "graphMode": "none", "textMode": "value_and_name",
                       "justifyMode": "center", "orientation": "vertical"}),
        row("PostgreSQL", 4),
        stat("Taille de la base", (0, 5, 6, 5), prom(f"pg_database_size_bytes{{{db}}}", instant=True),
             unit="bytes", thresholds=steps((None, BLUE))),
        timeseries("Connexions ouvertes", (6, 5, 9, 8),
                   [prom(f"pg_stat_database_numbackends{{{db}}}", "connexions")], unit="none"),
        timeseries("Transactions par seconde", (15, 5, 9, 8), [
            prom(f"rate(pg_stat_database_xact_commit{{{db}}}[5m])", "validées", "A"),
            prom(f"rate(pg_stat_database_xact_rollback{{{db}}}[5m])", "annulées", "B")], unit="ops"),
        row("Services", 13),
        timeseries("MLflow : requêtes par seconde", (0, 14, 12, 8),
                   [prom("sum by (status) (rate(mlflow_http_request_total[5m]))", "HTTP {{status}}")],
                   unit="reqps"),
        timeseries("Mémoire utilisée par service", (12, 14, 12, 8),
                   [prom("process_resident_memory_bytes", "{{job}}")], unit="bytes"),
        row("API de prévision", 22),
        timeseries("Requêtes par seconde", (0, 23, 8, 8),
                   [prom("sum by (route) (rate(smartsupply_api_requests_total[5m]))", "{{route}}")],
                   unit="reqps"),
        timeseries("Temps de réponse (95 % des requêtes en moins de...)", (8, 23, 8, 8),
                   [prom("histogram_quantile(0.95, sum by (le, route) "
                         "(rate(smartsupply_api_request_duration_seconds_bucket[5m])))", "{{route}}")],
                   unit="s", thresholds=steps((None, "transparent"), (2, RED)), threshold_lines=True),
        panel("stat", "Modèle servi par l'API", (16, 23, 8, 8),
              [prom("smartsupply_api_model_info", "{{source}} · version {{mlflow_version}} · {{run_id}}",
                    instant=True)],
              thresholds=steps((None, BLUE)), mappings=text_map({"1": ("en service", BLUE)}),
              options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "colorMode": "background", "graphMode": "none", "textMode": "value_and_name",
                       "justifyMode": "center", "orientation": "vertical"}),
    ]
    return dashboard("ss-infra", "SmartSupply — Infrastructure", p, time_from="now-24h",
                     description="Les briques techniques répondent-elles ? Base, MLflow, Prefect, API, mémoire.")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("cycle", cycle), ("modele", modele), ("infrastructure", infra)):
        path = OUT / f"smartsupply-{name}.json"
        path.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"écrit : {path.relative_to(OUT.parents[2])}")


if __name__ == "__main__":
    main()
