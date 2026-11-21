"""Construit l'historique depuis le 01/01/2024 jusqu'à HIER (calendrier réel).

    python -m smartsupply.generator.bootstrap

Relançable : s'il est interrompu (Ctrl+C, coupure), il reprend au jour suivant
le dernier jour enregistré, sans doublon. Il ne crée jamais de date future :
la dernière journée simulée est toujours une journée terminée (hier, en UTC).
"""
import argparse
import time
from datetime import timedelta

from smartsupply.db.connection import get_engine
from smartsupply.generator.reference_data import SEED
from smartsupply.generator.runner import last_complete_day, next_day_to_simulate, simulate_next_day


def main() -> None:
    parser = argparse.ArgumentParser(description="Génère l'historique jusqu'à hier.")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    engine = get_engine()
    until = last_complete_day()
    day = next_day_to_simulate(engine)
    if day > until:
        print(f"Historique déjà à jour (dernier jour terminé : {until}).")
        return

    print(f"Simulation du {day} au {until} (inclus)...")
    t0 = time.perf_counter()
    totals = {"orders": 0, "requested": 0, "lost": 0}
    while day <= until:
        summary = simulate_next_day(engine, seed=args.seed, mode="bootstrap", until=until)
        totals["orders"] += summary.orders
        totals["requested"] += summary.requested
        totals["lost"] += summary.lost
        if summary.day.day == 1:          # un point d'étape par mois
            print(summary)
        day = summary.day + timedelta(days=1)

    service = 100 * (1 - totals["lost"] / totals["requested"]) if totals["requested"] else 100
    print(
        f"Terminé en {time.perf_counter() - t0:.0f} s : {totals['orders']} commandes, "
        f"{totals['requested']} unités demandées, taux de service {service:.1f} %"
    )


if __name__ == "__main__":
    main()
