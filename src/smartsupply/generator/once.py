"""Simule la prochaine journée TERMINÉE, s'il y en a une. Utile pour tester / déboguer.

    python -m smartsupply.generator.once
"""
import argparse

from smartsupply.db.connection import get_engine
from smartsupply.generator.reference_data import SEED
from smartsupply.generator.runner import NothingToSimulate, simulate_next_day


def main() -> None:
    parser = argparse.ArgumentParser(description="Simule la journée suivante si elle est terminée.")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    try:
        print(simulate_next_day(get_engine(), seed=args.seed, mode="once"))
    except NothingToSimulate as exc:
        print(f"Rien à simuler : {exc}")


if __name__ == "__main__":
    main()
