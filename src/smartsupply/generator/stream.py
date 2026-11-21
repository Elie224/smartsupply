"""Boucle locale de rattrapage : vérifie toutes les N secondes s'il y a une journée
terminée à simuler (calendrier réel). En production, c'est le pipeline Prefect qui le fait.

    python -m smartsupply.generator.stream --seconds 60

Ctrl+C pour arrêter : une journée est soit entièrement enregistrée, soit annulée.
"""
import argparse
import time

from smartsupply.db.connection import get_engine
from smartsupply.generator.reference_data import SEED
from smartsupply.generator.runner import simulate_pending_days


def main() -> None:
    parser = argparse.ArgumentParser(description="Rattrapage continu des journées terminées.")
    parser.add_argument("--seconds", type=float, default=60.0, help="intervalle entre deux vérifications")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    engine = get_engine()
    print(f"Vérification toutes les {args.seconds:g} s (Ctrl+C pour arrêter)")
    try:
        while True:
            done = simulate_pending_days(engine, seed=args.seed, mode="stream")
            for summary in done:
                print(summary, flush=True)
            if not done:
                print("Rien de nouveau : la journée en cours n'est pas terminée.", flush=True)
            time.sleep(args.seconds)
    except KeyboardInterrupt:
        print("\nArrêté.")


if __name__ == "__main__":
    main()
