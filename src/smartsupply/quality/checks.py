"""Contrôles de qualité exécutés après chaque ingestion.

Chaque contrôle est une requête SQL qui compte les lignes EN ERREUR :
0 = OK. Si un contrôle échoue, le pipeline s'arrête en erreur (visible dans Prefect).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

CHECKS: dict[str, str] = {
    "soldes_egaux_au_journal": """
        SELECT count(*) FROM raw.inventory_balances b
        JOIN (SELECT product_id, warehouse_id, sum(quantity_delta) AS total
              FROM raw.stock_movements GROUP BY 1, 2) m USING (product_id, warehouse_id)
        WHERE m.total <> b.on_hand_qty
    """,
    "aucun_stock_negatif": """
        SELECT count(*) FROM raw.inventory_balances WHERE on_hand_qty < 0
    """,
    "jours_simules_consecutifs": """
        SELECT CASE WHEN count(*) = 0 THEN 0
                    ELSE (max(simulated_date) - min(simulated_date) + 1) - count(*) END
        FROM raw.simulation_runs
    """,
    "commandes_apres_inscription_client": """
        SELECT count(*) FROM raw.orders o JOIN raw.clients c USING (client_id)
        WHERE o.ordered_at < c.created_at
    """,
    "quantites_coherentes": """
        SELECT count(*) FROM raw.order_items
        WHERE fulfilled_qty > requested_qty
           OR lost_sales_qty <> requested_qty - fulfilled_qty
    """,
    "receptions_apres_commande": """
        SELECT count(*) FROM raw.purchase_orders WHERE received_at <= ordered_at
    """,
}


class DataQualityError(RuntimeError):
    pass


def run_quality_checks(engine: Engine) -> dict[str, int]:
    """Exécute tous les contrôles. Lève DataQualityError si l'un échoue."""
    with engine.connect() as conn:
        results = {name: conn.execute(text(sql)).scalar_one() for name, sql in CHECKS.items()}
    failed = {name: n for name, n in results.items() if n}
    if failed:
        raise DataQualityError(f"Contrôles qualité en échec : {failed}")
    return results
