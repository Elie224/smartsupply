"""Construction du dataset ML (README section 7).

Grain : une ligne = un produit observé à la FIN du jour D (as_of_date).
Cible : target_demand_next_7d = unités DEMANDÉES (requested_qty, pas les ventes)
        pendant D+1 ... D+7. NULL tant que ces 7 jours ne sont pas terminés.

Règle anti-fuite : chaque variable ne dépend que d'événements datés <= D.
  - lags / moyennes mobiles : fenêtres qui se terminent à D ;
  - promotions : seulement celles ANNONCÉES au plus tard à D ;
  - réceptions : commandes passées <= D et pas encore reçues à D ;
  - stock : somme des mouvements <= D.
Prouvé par tests/unit/test_features.py (troncature).

Usage :
    python -m smartsupply.features.build              # met à jour ml.feature_snapshots
    python -m smartsupply.features.build --export     # + exporte un dataset versionné (Parquet)
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import PROJECT_ROOT, get_engine
from smartsupply.features.extract import FeatureInputs, load_inputs

HORIZON_DAYS = 7
MIN_HISTORY_DAYS = 28      # il faut 28 jours d'historique pour les moyennes à 28 jours
TARGET = "target_demand_next_7d"
KEYS = ["as_of_date", "product_id"]
CATEGORICAL = ["category", "supplier_id"]
FEATURES = [
    "demand_lag_1", "demand_lag_7", "demand_lag_14", "demand_lag_28",
    "demand_rolling_mean_7", "demand_rolling_mean_28", "demand_rolling_std_28",
    "lost_sales_rolling_7",
    "price_as_of", "promotion_active", "discount_pct",
    "promo_days_next_7", "promo_max_discount_next_7",
    "weekday", "month", "day_of_year", "is_weekend",
    "stock_as_of", "incoming_qty_known_as_of", "supplier_lead_time_known_as_of",
    # historique long et saisonnalité (NaN tant que l'historique est insuffisant)
    "demand_rolling_mean_91", "demand_rolling_mean_364",
    "demand_next7_last_year",
    "category_season_index", "category_trend_7_28",
]
# Ces variables peuvent être vides au début de l'historique (moins d'un an de recul) :
# LightGBM sait gérer les valeurs manquantes.
NULLABLE_FEATURES = ["demand_rolling_mean_364", "demand_next7_last_year", "category_season_index"]
ALL_COLUMNS = KEYS + CATEGORICAL + FEATURES + [TARGET]


def _wide(df: pd.DataFrame, value: str, days: pd.DatetimeIndex, products: list[int]) -> pd.DataFrame:
    """Tableau jours x produits, 0 quand il ne s'est rien passé."""
    if df.empty:
        return pd.DataFrame(0.0, index=days, columns=products)
    table = df.pivot_table(index="day", columns="product_id", values=value, aggfunc="sum")
    table.index = pd.to_datetime(table.index)
    return table.reindex(index=days, columns=products, fill_value=0).fillna(0)


def _interval_sum(rows: list[tuple[date, date, int, float]], days: pd.DatetimeIndex,
                  products: list[int]) -> pd.DataFrame:
    """Somme de valeurs actives sur des intervalles [début, fin] (inclus), par produit.

    Technique du "tableau de différences" : +v au début, -v le lendemain de la fin,
    puis somme cumulée.
    """
    diff = pd.DataFrame(0.0, index=days, columns=products)
    first, last = days[0].date(), days[-1].date()
    for start, end, product_id, value in rows:
        start, end = max(start, first), min(end, last)
        if start > end or product_id not in diff.columns:
            continue
        diff.at[pd.Timestamp(start), product_id] += value
        after = end + timedelta(days=1)
        if after <= last:
            diff.at[pd.Timestamp(after), product_id] -= value
    return diff.cumsum()


def build_features(inputs: FeatureInputs) -> pd.DataFrame:
    days = pd.date_range(inputs.first_day, inputs.last_day, freq="D")
    products = inputs.products["product_id"].tolist()
    p = inputs.products.set_index("product_id")
    frames: dict[str, pd.DataFrame] = {}

    # --- Demande et ruptures --------------------------------------------------
    demand = _wide(inputs.demand, "requested_qty", days, products)
    lost = _wide(inputs.demand, "lost_sales_qty", days, products)
    frames["demand_lag_1"] = demand
    frames["demand_lag_7"] = demand.shift(6)
    frames["demand_lag_14"] = demand.shift(13)
    frames["demand_lag_28"] = demand.shift(27)
    frames["demand_rolling_mean_7"] = demand.rolling(7).mean()
    frames["demand_rolling_mean_28"] = demand.rolling(28).mean()
    frames["demand_rolling_std_28"] = demand.rolling(28).std()
    frames["lost_sales_rolling_7"] = lost.rolling(7).sum()
    frames["demand_rolling_mean_91"] = demand.rolling(91, min_periods=28).mean()
    frames["demand_rolling_mean_364"] = demand.rolling(364, min_periods=182).mean()
    # Demande de la semaine qui correspond, l'an dernier, à D+1..D+7 :
    # jours D-363..D-357, soit une somme glissante sur 7 jours décalée de 357 jours.
    weekly = demand.rolling(7).sum()
    frames["demand_next7_last_year"] = weekly.shift(357)

    # --- Signaux au niveau CATÉGORIE (plus de volume => moins de bruit) --------
    cat_of = p["category"]
    cat_daily = demand.T.groupby(cat_of.reindex(demand.columns).values).sum().T   # jours x catégories
    cat_weekly = cat_daily.rolling(7).sum()
    # Saisonnalité : même semaine l'an dernier / semaine moyenne de l'année écoulée (1 = normal)
    cat_season = cat_weekly.shift(357) / (7 * cat_daily.rolling(364, min_periods=300).mean())
    # Tendance récente de la catégorie : moyenne 7 j / moyenne 28 j
    cat_trend = cat_daily.rolling(7).mean() / cat_daily.rolling(28).mean()
    frames["category_season_index"] = cat_season[cat_of.reindex(products).values].set_axis(products, axis=1)
    frames["category_trend_7_28"] = cat_trend[cat_of.reindex(products).values].set_axis(products, axis=1)
    # Cible : somme D+1..D+7 = somme glissante sur 7 jours, décalée de 7 vers le passé.
    # Au-delà du dernier jour connu, shift() donne NaN : cible inconnue.
    frames[TARGET] = demand.rolling(HORIZON_DAYS).sum().shift(-HORIZON_DAYS)

    # --- Stock en fin de journée D -------------------------------------------
    frames["stock_as_of"] = _wide(inputs.movements, "quantity_delta", days, products).cumsum()

    # --- Réceptions CONNUES à D, attendues d'ici D+7 --------------------------
    # Comptée à D si : commandée <= D, pas encore reçue à D, et attendue <= D+7
    #   <=> D dans [max(ordered_at, expected_at - 7), received_at - 1]
    rows = []
    for r in inputs.purchases.itertuples(index=False):
        start = max(r.ordered_at, r.expected_at - timedelta(days=HORIZON_DAYS))
        received = None if pd.isna(r.received_at) else r.received_at
        end = (received - timedelta(days=1)) if received else inputs.last_day
        rows.append((start, end, r.product_id, float(r.qty)))
    frames["incoming_qty_known_as_of"] = _interval_sum(rows, days, products)

    # --- Promotions CONNUES à D ----------------------------------------------
    active = pd.DataFrame(0.0, index=days, columns=products)
    promo_days = pd.DataFrame(0.0, index=days, columns=products)
    promo_max = pd.DataFrame(0.0, index=days, columns=products)
    for r in inputs.promotions.itertuples(index=False):
        if r.product_id not in active.columns:
            continue
        # La promo influence les jours D où elle est connue et pas encore finie.
        d = r.announced_at
        while d <= min(r.ends_at, inputs.last_day):
            if d >= inputs.first_day:
                ts = pd.Timestamp(d)
                if r.starts_at <= d <= r.ends_at:
                    active.at[ts, r.product_id] = r.discount_pct
                window_start, window_end = d + timedelta(days=1), d + timedelta(days=HORIZON_DAYS)
                overlap = (min(window_end, r.ends_at) - max(window_start, r.starts_at)).days + 1
                if overlap > 0:
                    promo_days.at[ts, r.product_id] += overlap
                    promo_max.at[ts, r.product_id] = max(promo_max.at[ts, r.product_id], r.discount_pct)
            d += timedelta(days=1)
    frames["discount_pct"] = active
    frames["promo_days_next_7"] = promo_days
    frames["promo_max_discount_next_7"] = promo_max

    # --- Passage au format long (une ligne par produit et par jour) ----------
    long = pd.concat(
        {name: f.stack(future_stack=True) for name, f in frames.items()}, axis=1
    ).reset_index()
    long = long.rename(columns={"level_0": "as_of_date", "level_1": "product_id"})
    long.columns = ["as_of_date", "product_id", *frames.keys()]

    long["category"] = long["product_id"].map(p["category"])
    long["supplier_id"] = long["product_id"].map(p["supplier_id"]).astype(int)
    long["price_as_of"] = long["product_id"].map(p["unit_price"]).astype(float)
    long["supplier_lead_time_known_as_of"] = long["product_id"].map(p["lead_time_days"]).astype(int)
    long["promotion_active"] = long["discount_pct"] > 0
    long["weekday"] = long["as_of_date"].dt.day_of_week
    long["month"] = long["as_of_date"].dt.month
    long["day_of_year"] = long["as_of_date"].dt.day_of_year
    long["is_weekend"] = long["weekday"] >= 5

    # On écarte les jours sans 28 jours d'historique complets.
    long = long[long["as_of_date"] >= pd.Timestamp(inputs.first_day) + pd.Timedelta(days=MIN_HISTORY_DAYS - 1)]

    int_cols = [
        "demand_lag_1", "demand_lag_7", "demand_lag_14", "demand_lag_28",
        "lost_sales_rolling_7", "stock_as_of", "incoming_qty_known_as_of", "promo_days_next_7",
    ]
    long[int_cols] = long[int_cols].round().astype(int)
    long[TARGET] = long[TARGET].round().astype("Int64")
    long["as_of_date"] = long["as_of_date"].dt.date
    for col in NULLABLE_FEATURES:
        long[col] = long[col].astype(float)
    return long[ALL_COLUMNS].sort_values(KEYS).reset_index(drop=True)


# -----------------------------------------------------------------------------
# Écriture et export
# -----------------------------------------------------------------------------
def save_snapshots(engine: Engine, df: pd.DataFrame) -> int:
    """Remplace le contenu de ml.feature_snapshots (reconstruction complète, atomique).

    Reconstruire tout est simple et sûr : les cibles des 7 derniers jours se
    remplissent au fur et à mesure que les jours passent.
    """
    records = df.astype(object).where(df.notna(), None).to_dict("records")
    cols = ", ".join(ALL_COLUMNS)
    values = ", ".join(f":{c}" for c in ALL_COLUMNS)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ml.feature_snapshots"))
        conn.execute(text(f"INSERT INTO ml.feature_snapshots ({cols}) VALUES ({values})"), records)
    return len(records)


def content_hash(df: pd.DataFrame) -> str:
    """Empreinte du contenu : même snapshot de base => même empreinte."""
    hashed = pd.util.hash_pandas_object(df[ALL_COLUMNS], index=False).values
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _code_version() -> str:
    try:
        return version("smartsupply")
    except PackageNotFoundError:     # code lancé sans `pip install -e .`
        return "dev"


def export_dataset(engine: Engine, df: pd.DataFrame, out_dir: Path | None = None) -> Path:
    """Exporte les lignes ÉTIQUETÉES (cible connue) en Parquet + les enregistre dans le registre."""
    labeled = df[df[TARGET].notna()].reset_index(drop=True)
    digest = content_hash(labeled)
    as_of_max = labeled["as_of_date"].max()
    dataset_id = f"features_{as_of_max:%Y%m%d}_{digest[:8]}"
    out_dir = out_dir or PROJECT_ROOT / "data" / "datasets"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{dataset_id}.parquet"
    labeled.to_parquet(path, index=False)

    with engine.begin() as conn:
        seed = conn.execute(text("SELECT seed FROM raw.simulation_runs LIMIT 1")).scalar()
        conn.execute(
            text(
                """
                INSERT INTO ml.training_datasets
                    (dataset_id, as_of_min, as_of_max, n_rows, n_products, feature_columns,
                     target_column, content_hash, file_path, code_version, simulation_seed)
                VALUES (:id, :mn, :mx, :n, :np, :fc, :tc, :h, :fp, :cv, :seed)
                ON CONFLICT (dataset_id) DO NOTHING
                """
            ),
            {
                "id": dataset_id, "mn": labeled["as_of_date"].min(), "mx": as_of_max,
                "n": len(labeled), "np": labeled["product_id"].nunique(),
                "fc": CATEGORICAL + FEATURES, "tc": TARGET, "h": digest,
                "fp": str(path), "cv": _code_version(), "seed": seed,
            },
        )
    path.with_suffix(".json").write_text(
        json.dumps(
            {"dataset_id": dataset_id, "content_hash": digest, "rows": len(labeled),
             "as_of_min": str(labeled["as_of_date"].min()), "as_of_max": str(as_of_max),
             "features": CATEGORICAL + FEATURES, "target": TARGET},
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def summary(df: pd.DataFrame) -> str:
    labeled = df[df[TARGET].notna()]
    corr = labeled[FEATURES[:8] + [TARGET]].astype(float).corr()[TARGET].drop(TARGET)
    lines = [
        f"Lignes : {len(df)}  (avec cible : {len(labeled)}, cible encore inconnue : {len(df) - len(labeled)})",
        f"Produits : {df['product_id'].nunique()}  |  dates : {df['as_of_date'].min()} -> {df['as_of_date'].max()}",
        f"Cible (demande 7 j) : moyenne {labeled[TARGET].mean():.1f}, médiane {labeled[TARGET].median():.0f}, "
        f"max {labeled[TARGET].max()}, part de zéros {100 * (labeled[TARGET] == 0).mean():.1f} %",
        "Corrélation avec la cible :",
        *(f"   {name:<26} {value:+.2f}" for name, value in corr.sort_values(ascending=False).items()),
    ]
    return "\n".join(lines)


def build_and_save(engine: Engine | None = None, export: bool = False) -> dict:
    engine = engine or get_engine()
    df = build_features(load_inputs(engine))
    result = {"rows": save_snapshots(engine, df)}
    if export:
        result["dataset"] = str(export_dataset(engine, df))
    result["summary"] = summary(df)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Construit le dataset ML depuis PostgreSQL.")
    parser.add_argument("--export", action="store_true", help="exporte aussi un dataset versionné (Parquet)")
    args = parser.parse_args()
    result = build_and_save(export=args.export)
    print(f"ml.feature_snapshots : {result['rows']} lignes")
    if "dataset" in result:
        print(f"Dataset exporté : {result['dataset']}")
    print(result["summary"])


if __name__ == "__main__":
    main()
