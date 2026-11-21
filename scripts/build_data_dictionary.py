"""Génère docs/data_dictionary.md : toutes les tables et vues de la base, colonne par colonne.

    python scripts/build_data_dictionary.py

Sources, pour que le document reste juste quand le schéma évolue :
    - la base elle-même : colonnes, types, NULL autorisé, clés, contraintes, volumes ;
    - les commentaires des fichiers database/schema.sql et database/analytics_views.sql
      (en-tête de chaque table/vue, commentaire en fin de ligne de chaque colonne) ;
    - quelques descriptions écrites ici (variables du modèle, colonnes communes).
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from smartsupply.db.connection import get_engine  # noqa: E402

OUT = ROOT / "docs" / "data_dictionary.md"
SCHEMAS = {
    "raw": "Données brutes écrites par le simulateur (le « système opérationnel » de l'entreprise fictive).",
    "ml": "Machine learning : variables du modèle, entraînements, prévisions, surveillance.",
    "ops": "Exploitation : journal du cycle automatique.",
    "analytics": "Vues de lecture (aucune donnée copiée) pour Power BI, Grafana, l'API et le site.",
}

# Descriptions écrites à la main, prioritaires sur les commentaires SQL quand elles existent.
COMMON = {
    "ingested_at": "Instant réel d'insertion en base (horloge du PC, pas la date simulée).",
    "product_id": "Produit (raw.products).",
    "warehouse_id": "Entrepôt (raw.warehouses).",
    "supplier_id": "Fournisseur (raw.suppliers).",
    "client_id": "Client (raw.clients).",
    "category": "Catégorie du produit.",
    "sku": "Référence article.",
}
# Par nom de colonne, quel que soit l'objet (après les descriptions propres à un objet).
BY_NAME = {
    "name": "Nom.",
    "city": "Ville.",
    "country": "Pays.",
    "segment": "B2B (entreprise) ou B2C (particulier).",
    "active": "Produit encore vendu.",
    "on_hand_qty": "Unités en stock dans l'entrepôt.",
    "order_id": "Commande client (raw.orders).",
    "order_item_id": "Identifiant de la ligne de commande.",
    "ordered_at": "Date de la commande (simulée).",
    "channel": "Canal de vente : web, store (magasin) ou b2b_portal.",
    "promotion_id": "Identifiant de la promotion.",
    "starts_at": "Début de la promotion.",
    "ends_at": "Fin de la promotion.",
    "announced_at": "Date à laquelle la promotion est connue (une promotion n'est utilisée par le modèle qu'une fois annoncée).",
    "discount_pct": "Remise, en fraction (0,20 = 20 %).",
    "po_id": "Commande fournisseur (raw.purchase_orders).",
    "po_item_id": "Identifiant de la ligne de commande fournisseur.",
    "ordered_qty": "Unités commandées.",
    "received_qty": "Unités reçues (vide tant que la commande est en transit).",
    "unit_cost": "Coût d'achat unitaire (EUR).",
    "movement_id": "Identifiant du mouvement de stock.",
    "movement_type": "initial (stock de départ), receipt (réception fournisseur) ou sale (vente).",
    "quantity_delta": "Variation de stock : + entrée, − sortie.",
    "seed": "Graine aléatoire (rejouer la simulation à l'identique).",
    "mode": "Origine : bootstrap (historique), once, stream, pipeline (cycle de nuit) ou scenario.",
    "simulated_date": "Journée simulée (jamais deux fois la même).",
    "started_at": "Début.",
    "finished_at": "Fin.",
    "trained_at": "Date de l'entraînement.",
    "eval_end": "Fin de la fenêtre d'évaluation (8 semaines).",
    "bias": "Biais sur la fenêtre d'évaluation (> 0 : surestimation).",
    "champion_wape": "Erreur du modèle en place au moment de la décision.",
    "level": "stable / à surveiller / forte (seuils du PSI).",
    "computed_at": "Instant du calcul.",
    "updated_at": "Dernière mise à jour.",
    "as_of_min": "Première journée du jeu de données.",
    "as_of_max": "Dernière journée du jeu de données.",
    "n_rows": "Nombre de lignes.",
    "n_products": "Nombre de produits.",
    "feature_columns": "Variables incluses.",
    "target_column": "Colonne cible.",
    "file_path": "Fichier exporté (data/datasets).",
    "code_version": "Version du code qui a produit le fichier.",
    "simulation_seed": "Graine de la simulation.",
    "days_added": "Journées simulées pendant le passage.",
    "model_status": "Résultat de l'étape modèle (mis en service, refusé, déjà fait...).",
    "forecast_status": "Résultat de l'étape prévision.",
    "sales_date": "Jour des ventes.",
    "product_name": "Nom du produit.",
    "warehouse_name": "Nom de l'entrepôt.",
    "supplier_name": "Nom du fournisseur.",
    "n_clients": "Clients distincts.",
    "lost_revenue_eur": "CA perdu à cause des ruptures (unités non livrées × prix).",
    "abs_error_qty": "Écart absolu prévu / réel (unités), quand le réel est connu.",
    "expected_at": "Date de livraison annoncée.",
    "first_sale_date": "Première vente.",
    "last_sale_date": "Dernière vente.",
    "gross_margin_pct": "Marge brute / CA, en %.",
    "promo_days": "Jours de promotion.",
    "revenue_rank": "Rang selon le CA (1 = le plus gros).",
    "duration_days": "Durée, en jours.",
    "in_transit_qty": "Unités commandées au fournisseur, pas encore reçues.",
    "next_expected_at": "Prochaine livraison attendue.",
    "supplier_lead_time_days": "Délai annoncé par le fournisseur (jours).",
    "avg_daily_demand_28d": "Demande moyenne par jour sur 28 jours.",
    "days_of_cover": "Jours de stock restants au rythme actuel (vide si aucune demande).",
    "stock_status": "rupture (stock nul) / critique (< 7 jours et pas de livraison à temps) / surstock (> 60 jours) / ok.",
    "stock_value_eur": "Valeur du stock au coût d'achat (EUR).",
    "announced_lead_time_days": "Délai annoncé par le fournisseur (jours).",
    "n_purchase_orders": "Commandes passées au fournisseur.",
    "n_received": "Commandes reçues.",
    "n_in_transit": "Commandes en transit.",
    "avg_actual_lead_time_days": "Délai réellement constaté, en moyenne (jours).",
    "avg_delay_days": "Retard moyen par rapport au délai annoncé (jours).",
    "late_rate_pct": "Part des commandes livrées en retard, en %.",
    "max_delay_days": "Plus gros retard constaté (jours).",
    "created_date": "Date d'inscription.",
    "first_order": "Première commande.",
    "last_order": "Dernière commande.",
    "days_without_order": "Jours depuis la dernière commande (ou l'inscription).",
    "client_status": "perdu (≥ 12 mois sans commande) / à risque (6 à 12 mois) / nouveau (< 90 jours) / actif.",
    "lost_date": "Date à laquelle le client est devenu perdu.",
    "margin_eur": "Marge brute totale (EUR).",
    "revenue_12m": "CA des 12 derniers mois (EUR).",
    "revenue_prev_12m": "CA des 12 mois précédents (EUR).",
    "margin_12m": "Marge des 12 derniers mois (EUR).",
    "margin_prev_12m": "Marge des 12 mois précédents (EUR).",
    "n_orders_12m": "Commandes sur 12 mois.",
    "revenue_change_pct": "Évolution du CA sur un an, en %.",
    "margin_change_pct": "Évolution de la marge sur un an, en %.",
    "value_class": "Classe ABC sur le CA 12 mois : A = 80 % du CA, B = 15 % suivants, C = le reste.",
    "revenue_trend": "en hausse (≥ +20 %) / en baisse (≤ −20 %) / stable / nouveau CA / sans historique.",
    "to_follow": "À suivre : client à risque, ou client A/B dont le CA baisse d'au moins 20 %.",
    "month": "Mois.",
    "new_clients": "Clients inscrits dans le mois.",
    "lost_clients": "Clients devenus perdus dans le mois (12 mois sans commande).",
    "active_clients_12m": "Clients avec au moins une commande sur les 12 mois précédents.",
    "last_simulated_day": "Dernière journée simulée.",
    "last_forecast_day": "Jour de la dernière prévision.",
    "last_training_data_until": "Données du dernier entraînement (jusqu'au).",
    "last_run_at": "Fin du dernier passage du cycle.",
    "last_run_status": "Résultat du dernier passage (ok / échec).",
    "last_success_at": "Fin du dernier passage réussi.",
    "hours_since_success": "Heures depuis la dernière réussite.",
    "status": "État.",
}
SPECIFIC = {
    "raw.simulation_runs": {"run_id": "Identifiant du passage de simulation.",
                            "status": "success ou failed."},
    "raw.orders": {"status": "fulfilled (tout livré) / partial (en partie) / unfulfilled (rien, rupture)."},
    "raw.purchase_orders": {"status": "in_transit (en route) ou received (reçue)."},
    "ml.training_datasets": {"created_at": "Date de l'export."},
    "ops.pipeline_runs": {"run_id": "Identifiant du passage.", "status": "ok ou échec."},
    "analytics.v_daily_sales": {"promotion_active": "Une promotion était en cours ce jour-là."},
    "analytics.v_pipeline_runs": {"status": "ok ou échec."},
    "ml.feature_snapshots": {
        "as_of_date": "Jour D : la ligne décrit le produit à la fin de ce jour, avec seulement ce qui était connu.",
        "demand_rolling_mean_7": "Demande moyenne des 7 derniers jours (D-6 à D).",
        "demand_rolling_mean_28": "Demande moyenne des 28 derniers jours.",
        "demand_rolling_std_28": "Écart-type de la demande sur 28 jours (régularité).",
        "lost_sales_rolling_7": "Unités demandées mais non servies (ruptures) sur 7 jours.",
        "price_as_of": "Prix de vente catalogue (EUR, avant remise ; la remise est dans discount_pct).",
        "promotion_active": "Promotion en cours le jour D.",
        "discount_pct": "Remise du jour D, en fraction (0,20 = 20 %).",
        "promo_days_next_7": "Jours de promotion déjà annoncés sur D+1..D+7.",
        "promo_max_discount_next_7": "Plus forte remise annoncée sur D+1..D+7 (fraction).",
        "month": "Mois de D (1 à 12).",
        "day_of_year": "Jour de l'année de D (1 à 366).",
        "is_weekend": "D est un samedi ou un dimanche.",
        "stock_as_of": "Stock disponible à la fin de D, tous entrepôts.",
        "supplier_lead_time_known_as_of": "Délai de livraison du fournisseur connu à D (jours).",
        "demand_rolling_mean_91": "Demande moyenne des 91 derniers jours (tendance de fond).",
        "demand_next7_last_year": "Demande de la semaine correspondante l'an dernier (jours D-363 à D-357) : saisonnalité annuelle.",
        "category_season_index": "Saisonnalité de la catégorie à cette période de l'année (1 = normal).",
        "category_trend_7_28": "Tendance récente de la catégorie : moyenne 7 jours / moyenne 28 jours.",
        "target_demand_next_7d": "CIBLE : unités demandées de D+1 à D+7. NULL tant que ces 7 jours ne sont pas passés.",
        "built_at": "Instant du calcul de la ligne.",
    },
    "ml.model_runs": {
        "run_id": "Identifiant de l'entraînement (horodatage UTC), aussi tag MLflow smartsupply_run_id.",
        "model_name": "Type de modèle (lightgbm).",
        "n_train_rows": "Lignes utilisées pour l'entraînement évalué.",
        "mae": "Erreur absolue moyenne sur la fenêtre d'évaluation (unités).",
        "promoted": "Mis en service (règle : bat la méthode naïve, |biais| ≤ 5 %, pas pire que le modèle en place).",
        "params": "Réglages LightGBM utilisés (JSON).",
        "withdrawn_at": "Retour arrière : date de retrait du service (NULL = jamais retiré).",
        "withdrawn_reason": "Retour arrière : raison du retrait.",
    },
    "ml.forecasts": {
        "as_of_date": "Dernière journée connue au moment de la prévision.",
        "horizon_start": "Premier jour prévu (as_of_date + 1).",
        "horizon_end": "Dernier jour prévu (as_of_date + 7).",
        "predicted_qty": "Unités demandées prévues sur les 7 jours.",
        "run_id": "Modèle qui a fait la prévision (ml.model_runs).",
        "created_at": "Instant où la prévision a été enregistrée.",
    },
    "ml.drift_metrics": {
        "as_of_date": "Jour de la mesure.",
        "feature": "Variable du modèle.",
        "psi": "Population Stability Index : < 0,10 stable, 0,10-0,25 à surveiller, > 0,25 forte.",
        "reference_start": "Début de la période de référence (année d'entraînement du modèle).",
        "reference_end": "Fin de la période de référence.",
        "current_start": "Début de la période récente comparée (28 jours).",
        "model_run_id": "Modèle en service ce jour-là.",
    },
    "ml.monitoring_daily": {
        "as_of_date": "Jour de surveillance (jour de la prévision).",
        "model_run_id": "Modèle en service ce jour-là.",
        "max_psi": "PSI le plus élevé du jour.",
        "top_feature": "Variable qui a le PSI le plus élevé.",
        "drift_logged": "Dérive déjà recopiée dans MLflow.",
        "real_bias": "Biais réel de la prévision de ce jour (> 0 : surestimation).",
        "real_logged": "Erreur réelle déjà recopiée dans MLflow.",
    },
}
VIEWS = {
    "is_latest": "Ligne de la prévision la plus récente (celle du jour).",
    "actual_qty": "Unités réellement demandées sur les 7 jours (vide tant qu'ils ne sont pas passés).",
    "predicted_revenue_eur": "CA prévu sur 7 jours = demande prévue × prix catalogue (EUR).",
    "stock_as_of": "Stock disponible à la fin de as_of_date, tous entrepôts.",
    "incoming_qty": "Réceptions fournisseurs attendues sur les 7 jours prévus.",
    "projected_stock_end": "Stock projeté en fin de semaine = stock + réceptions − demande prévue.",
    "forecast_alert": "ok / à surveiller (stock < 1,5 × demande prévue) / rupture probable (stock < demande prévue).",
    "model_run_id": "Modèle qui a fait la prévision (ml.model_runs).",
    "wape_pct": "Erreur en % : somme des écarts absolus / demande totale.",
    "baseline_wape_pct": "Erreur de la méthode naïve (moyenne 28 jours), même fenêtre, en %.",
    "bias_pct": "Biais en % (> 0 : le modèle surestime).",
    "champion_wape_pct": "Erreur du modèle en place au moment de la décision, en %.",
    "is_current": "Modèle actuellement en service (dernier retenu et non retiré).",
    "service_rate_pct": "Taux de service : unités livrées / unités demandées, en %.",
    "revenue_eur": "Chiffre d'affaires (EUR, remises comprises).",
    "gross_margin_eur": "Marge brute = CA − coût d'achat des unités vendues (EUR).",
    "requested_qty": "Unités demandées par les clients.",
    "fulfilled_qty": "Unités réellement livrées.",
    "lost_sales_qty": "Unités demandées mais non livrées (rupture).",
    "n_orders": "Nombre de commandes.",
    "n_active_clients": "Clients ayant commandé ce jour-là.",
    "health_status": "ok / en retard / échec / aucun passage.",
    "health_message": "Explication en clair et consigne à suivre.",
    "expected_day": "Journée qui devrait déjà être simulée (hier, ou avant-hier avant 4 h).",
    "duration_s": "Durée du passage, en secondes.",
}
USED_BY = {
    "v_daily_sales": "Power BI", "v_daily_kpis": "Power BI (Vue direction), site public",
    "v_product_performance": "Power BI", "v_stock_status": "Power BI", "v_supplier_reliability": "Power BI",
    "v_incoming_receipts": "Power BI", "v_promotions": "Power BI",
    "v_forecasts": "Power BI (Prévisions), API, site public, Prometheus",
    "v_forecast_accuracy": "Power BI, site public", "v_model_runs": "Power BI, Grafana, site public",
    "v_clients": "Power BI (Clients)", "v_clients_monthly": "Power BI (Clients)",
    "v_pipeline_health": "Power BI, alerte Windows, site public",
    "v_monitoring_daily": "Grafana, site public", "v_drift_metrics": "Grafana, site public",
    "v_pipeline_runs": "Grafana",
}


# -----------------------------------------------------------------------------
# Commentaires des fichiers SQL
# -----------------------------------------------------------------------------
HEADER = re.compile(r"^--\s*\d+(?:\s*bis)?\)\s*(.*)$")
OBJECT = re.compile(r"^(?:CREATE TABLE IF NOT EXISTS|CREATE OR REPLACE VIEW|ALTER TABLE)\s+(\w+\.\w+)")
COLUMN = re.compile(r"^\s*(?:ADD COLUMN IF NOT EXISTS\s+)?([a-z_][a-z0-9_]*)\s+[A-Z].*?--\s*(.+?)\s*$")
VIEW_COLUMN = re.compile(r"\bAS\s+([a-z_][a-z0-9_]*)\b[^-]*--\s*(.+?)\s*$")


def sql_comments(paths: list[Path]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """(description de chaque objet, commentaire de chaque colonne)."""
    descriptions: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = defaultdict(dict)
    for path in paths:
        header: list[str] = []
        collecting = False
        current = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.rstrip()
            m = HEADER.match(line)
            if m:
                header, collecting = [m.group(1)], True
                continue
            if collecting and line.startswith("--") and not set(line) <= set("- "):
                header.append(line.lstrip("-").strip())
                continue
            if collecting and set(line) <= set("- "):
                continue
            collecting = False
            m = OBJECT.match(line)
            if m:
                current = m.group(1)
                if not line.startswith("ALTER") and current not in descriptions and header:
                    descriptions[current] = _clean_header(header, current)
                continue
            if current:
                m = COLUMN.match(line) or VIEW_COLUMN.search(line)
                if m and not line.lstrip().startswith(("--", "CONSTRAINT")):
                    columns[current].setdefault(m.group(1), m.group(2))
    return descriptions, columns


def _clean_header(lines: list[str], name: str) -> str:
    first = re.sub(r"^[\w.]+(?:\s*/\s*[\w.]+)*\s*:\s*", "", lines[0])
    first = first[0].upper() + first[1:] if first else first
    return " ".join([first, *lines[1:]]).strip()


# -----------------------------------------------------------------------------
# Structure réelle dans la base
# -----------------------------------------------------------------------------
def database_structure(conn) -> tuple[list, dict, dict, dict]:
    objects = conn.execute(text("""
        SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'v')
        ORDER BY array_position(:s, n.nspname::text), c.relkind, c.relname"""), {"s": list(SCHEMAS)}).all()
    cols = defaultdict(list)
    for r in conn.execute(text("""
            SELECT n.nspname || '.' || c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(:s) AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attrelid, a.attnum"""), {"s": list(SCHEMAS)}):
        cols[r[0]].append((r[1], r[2], r[3]))
    keys = defaultdict(dict)        # objet -> colonne -> « PK » / « → raw.products »
    constraints = defaultdict(list)
    for r in conn.execute(text("""
            SELECT n.nspname || '.' || c.relname, con.contype, con.conname, pg_get_constraintdef(con.oid),
                   ARRAY(SELECT attname FROM pg_attribute WHERE attrelid = con.conrelid
                         AND attnum = ANY(con.conkey) ORDER BY attnum),
                   fn.nspname || '.' || fc.relname
            FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_class fc ON fc.oid = con.confrelid LEFT JOIN pg_namespace fn ON fn.oid = fc.relnamespace
            WHERE n.nspname = ANY(:s) ORDER BY 1, 2, 3"""), {"s": list(SCHEMAS)}):
        obj, kind, name, definition, cols_, target = r
        if kind == "p":
            for col in cols_:
                keys[obj][col] = "clé"
        elif kind == "f":
            for col in cols_:
                keys[obj][col] = f"→ {target}"
        elif kind in ("c", "u"):
            constraints[obj].append(definition)
    return objects, cols, keys, constraints


# -----------------------------------------------------------------------------
def describe(obj: str, column: str, sql_cols: dict) -> str:
    found = (SPECIFIC.get(obj, {}).get(column) or sql_cols.get(obj, {}).get(column)
             or COMMON.get(column))
    if not found and column in BY_NAME and column not in VIEWS:
        found = BY_NAME[column]
    if found or not obj.startswith("analytics."):
        return found or ""
    # Vue : description propre aux vues, sinon celle de la même colonne dans une table d'origine.
    if column in VIEWS:
        return VIEWS[column]
    if column in BY_NAME:
        return BY_NAME[column]
    for table in ("ml.forecasts", "ml.model_runs", "ml.monitoring_daily", "ml.drift_metrics",
                  "ops.pipeline_runs", "raw.products", "raw.clients", "raw.orders", "raw.order_items"):
        found = SPECIFIC.get(table, {}).get(column) or sql_cols.get(table, {}).get(column)
        if found:
            return found
    return ""


def md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def build() -> str:
    descriptions, sql_cols = sql_comments([ROOT / "database" / "schema.sql",
                                           ROOT / "database" / "analytics_views.sql"])
    with get_engine().connect() as conn:
        objects, cols, keys, constraints = database_structure(conn)
        exact = {f"{s}.{n}": conn.execute(text(f"SELECT count(*) FROM {s}.{n}")).scalar()
                 for s, n, kind, est in objects if kind == "r" and est < 2_000_000}
    out = [
        "# Dictionnaire de données — SmartSupply AI",
        "",
        f"*Généré le {datetime.now():%d/%m/%Y} par `python scripts/build_data_dictionary.py` à partir de la base "
        "et des commentaires de `database/*.sql` : ne pas modifier à la main.*",
        "",
        "**Données 100 % synthétiques.** Conventions : montants en EUR (`numeric(12,2)`), dates avec fuseau "
        "(`timestamptz`) ; `created_at` = date **métier simulée**, `ingested_at` = instant **réel** d'insertion ; "
        "remises stockées en fraction (0,20 = 20 %). Les vues `analytics` ne copient aucune donnée.",
        "",
        "## Sommaire",
        "",
    ]
    by_schema = defaultdict(list)
    for s, n, kind, est in objects:
        by_schema[s].append((n, kind, est))
    for s, items in by_schema.items():
        links = ", ".join(f"[{n}](#{s}{n})" for n, _, _ in items)
        out.append(f"- **{s}** — {SCHEMAS[s]} {links}")
    out.append("")
    for s, items in by_schema.items():
        out += [f"## Schéma `{s}`", "", SCHEMAS[s], ""]
        for n, kind, est in items:
            obj = f"{s}.{n}"
            out.append(f"### {obj}")
            out.append("")
            meta = []
            if kind == "r":
                volume = exact.get(obj)
                meta.append(f"Table — {volume:,} lignes".replace(",", " ") if volume is not None
                            else f"Table — environ {est:,} lignes".replace(",", " "))
            else:
                meta.append("Vue" + (f" — lue par : {USED_BY[n]}" if n in USED_BY else ""))
            out.append(f"*{meta[0]}*")
            out.append("")
            if descriptions.get(obj):
                out += [descriptions[obj], ""]
            out += ["| Colonne | Type | Obligatoire | Clé | Description |", "|---|---|---|---|---|"]
            for col, typ, notnull in cols[obj]:
                out.append(f"| `{col}` | {typ} | {'oui' if notnull else ''} | {keys[obj].get(col, '')} | "
                           f"{md_escape(describe(obj, col, sql_cols))} |")
            if constraints.get(obj):
                out += ["", "Règles vérifiées par la base :", ""]
                out += [f"- `{md_escape(c)}`" for c in constraints[obj]]
            out.append("")
    return "\n".join(out)


def main() -> None:
    OUT.write_text(build(), encoding="utf-8")
    print(f"écrit : {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
