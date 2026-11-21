"""Enchaîne la simulation jour après jour, en toute sécurité.

Chaque jour est simulé dans UNE transaction :
  - succès  -> événements + ligne simulation_runs enregistrés ensemble (COMMIT)
  - erreur / coupure -> rien n'est enregistré (ROLLBACK), on rejoue le même jour.
Le prochain jour à simuler est toujours  max(simulated_date) + 1  :
pas de doublon possible, même après un redémarrage.

Horloge : la simulation suit le CALENDRIER RÉEL. On ne simule qu'une journée
TERMINÉE (au plus tard hier, en UTC) : jamais de date dans le futur.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.generator import repository as repo
from smartsupply.generator.reference_data import HISTORY_START, SEED
from smartsupply.generator.simulator import initial_inventory, simulate_day

HISTORY_START_DATE: date = HISTORY_START.date()
# Verrou PostgreSQL : empêche deux simulateurs de tourner en même temps.
ADVISORY_LOCK_KEY = 42_4242


def last_complete_day(now: datetime | None = None) -> date:
    """Dernière journée terminée = hier (UTC). Aucune donnée ne peut dépasser cette date."""
    now = now or datetime.now(timezone.utc)
    return now.date() - timedelta(days=1)


class NothingToSimulate(Exception):
    """La prochaine journée n'est pas encore terminée."""


@dataclass(frozen=True)
class DaySummary:
    day: date
    orders: int
    requested: int
    fulfilled: int
    lost: int
    purchase_orders: int
    receipts: int

    def __str__(self) -> str:
        service = 100 * self.fulfilled / self.requested if self.requested else 100.0
        return (
            f"{self.day} | {self.orders:>4} commandes | demandé {self.requested:>5} "
            f"| servi {service:5.1f} % | perdu {self.lost:>4} "
            f"| {self.purchase_orders:>2} achats | {self.receipts:>2} réceptions"
        )


def next_day_to_simulate(engine: Engine) -> date:
    with engine.connect() as conn:
        last = repo.last_simulated_date(conn)
    return HISTORY_START_DATE if last is None else last + timedelta(days=1)


def simulate_next_day(
    engine: Engine, seed: int = SEED, mode: str = "once", until: date | None = None
) -> DaySummary:
    """Simule la journée suivante, à condition qu'elle soit terminée (<= until, hier par défaut)."""
    started_at = datetime.now(timezone.utc)
    until = until or last_complete_day()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        repo.check_seed(conn, seed)

        last = repo.last_simulated_date(conn)
        day = HISTORY_START_DATE if last is None else last + timedelta(days=1)
        if day > until:
            raise NothingToSimulate(f"La journée du {day} n'est pas encore terminée (dernière : {until}).")

        profiles = repo.load_profiles(conn, day, seed)
        shares = repo.load_warehouse_shares(conn)
        clients = repo.load_clients(conn, day, shares, seed, HISTORY_START_DATE)
        if not (profiles and shares and clients):
            raise RuntimeError(
                "Données de référence absentes : lance d'abord "
                "`python -m smartsupply.generator.reference_data`."
            )

        state = repo.load_state(conn, day)
        if not state.balances:   # tout premier jour : stock initial
            balances, movements = initial_inventory(profiles, shares, day, state.next_ids)
            repo.save_initial_inventory(conn, balances, movements, day)
            state.balances = balances

        result, _ = simulate_day(state, profiles, shares, clients, seed, HISTORY_START_DATE)
        repo.save_day(conn, result)
        repo.sync_identity_sequences(conn)
        repo.record_run(conn, seed, mode, day, started_at)

    requested = sum(i["requested_qty"] for i in result.order_items)
    fulfilled = sum(i["fulfilled_qty"] for i in result.order_items)
    return DaySummary(
        day=day,
        orders=len(result.orders),
        requested=requested,
        fulfilled=fulfilled,
        lost=requested - fulfilled,
        purchase_orders=len(result.purchase_orders),
        receipts=len(result.received_po_ids),
    )


def simulate_pending_days(
    engine: Engine, seed: int = SEED, mode: str = "pipeline", until: date | None = None
) -> list[DaySummary]:
    """Rattrapage : simule toutes les journées terminées qui manquent (0, 1 ou plusieurs).

    Si le PC était éteint pendant 3 jours, les 3 jours sont simulés au prochain run.
    """
    until = until or last_complete_day()
    summaries = []
    while True:
        try:
            summaries.append(simulate_next_day(engine, seed=seed, mode=mode, until=until))
        except NothingToSimulate:
            return summaries
