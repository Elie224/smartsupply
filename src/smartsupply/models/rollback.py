"""Retour arrière du modèle en service (rollback).

Le modèle en service est le dernier entraînement RETENU et NON RETIRÉ (ml.model_runs).
Retirer un modèle remet automatiquement en service le précédent modèle retenu :
le cycle de nuit, l'API, MLflow (alias « champion »), le site public et Prometheus
suivent tous cette même règle. Rien n'est effacé : un retrait s'annule.

    python -m smartsupply.models.rollback                       # liste des modèles retenus
    python -m smartsupply.models.rollback --precedent --raison "erreur réelle trop haute"
    python -m smartsupply.models.rollback --vers 20261121T125301Z --raison "..."
    python -m smartsupply.models.rollback --annuler 20261122T013000Z

Après le changement, MLflow et le site public sont mis à jour tout de suite (si joignables) ;
l'API locale recharge le modèle en moins de 5 minutes. Les prévisions déjà faites ne sont
jamais modifiées : la prochaine prévision (cette nuit) utilisera le modèle remis en service.

Le cycle de nuit continue normalement : un nouveau modèle ne sera mis en service que s'il
fait au moins aussi bien que le modèle remis en service (même règle que d'habitude).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import get_engine


class RollbackError(ValueError):
    """Retour arrière impossible (message lisible)."""


@dataclass(frozen=True)
class Model:
    run_id: str
    trained_at: datetime
    data_until: object
    wape: float
    baseline_wape: float
    withdrawn_at: datetime | None
    withdrawn_reason: str | None

    @property
    def withdrawn(self) -> bool:
        return self.withdrawn_at is not None


def promoted_models(engine: Engine) -> list[Model]:
    """Modèles retenus, du plus récent au plus ancien."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT run_id, trained_at, data_until, wape, baseline_wape, withdrawn_at, withdrawn_reason "
            "FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC")).all()
    return [Model(*r) for r in rows]


def current(models: list[Model]) -> Model | None:
    return next((m for m in models if not m.withdrawn), None)


def plan(models: list[Model], target: str | None = None) -> tuple[Model, list[Model]]:
    """(modèle qui sera en service, modèles à retirer). Fonction pure, testée sans base.

    target=None : revenir au modèle retenu précédent.
    target=run_id : revenir à ce modèle (tous les modèles en service plus récents sont retirés)."""
    active = [m for m in models if not m.withdrawn]
    if not active:
        raise RollbackError("aucun modèle en service")
    if target is None:
        if len(active) < 2:
            raise RollbackError(f"pas de modèle précédent : {active[0].run_id} est le seul modèle retenu "
                                "non retiré")
        return active[1], [active[0]]
    chosen = next((m for m in models if m.run_id == target), None)
    if chosen is None:
        raise RollbackError(f"{target} n'est pas un modèle retenu (voir la liste)")
    if chosen.withdrawn:
        raise RollbackError(f"{target} est retiré : l'annuler d'abord avec --annuler {target}")
    newer = [m for m in active if m.trained_at > chosen.trained_at]
    if not newer:
        raise RollbackError(f"{target} est déjà le modèle en service")
    return chosen, newer


def withdraw(engine: Engine, run_ids: list[str], reason: str) -> None:
    with engine.begin() as conn:
        for run_id in run_ids:
            conn.execute(text("UPDATE ml.model_runs SET withdrawn_at = now(), withdrawn_reason = :r "
                              "WHERE run_id = :id AND promoted AND withdrawn_at IS NULL"),
                         {"id": run_id, "r": reason})


def restore(engine: Engine, run_id: str) -> None:
    with engine.begin() as conn:
        updated = conn.execute(text("UPDATE ml.model_runs SET withdrawn_at = NULL, withdrawn_reason = NULL "
                                    "WHERE run_id = :id AND withdrawn_at IS NOT NULL"), {"id": run_id}).rowcount
    if not updated:
        raise RollbackError(f"{run_id} n'est pas un modèle retiré")


def propagate(engine: Engine) -> dict[str, str]:
    """Fait suivre MLflow (alias champion) et le site public tout de suite. Jamais bloquant."""
    out = {}
    try:
        from smartsupply.models.tracking import sync_all
        out["MLflow"] = sync_all(engine)["status"]
    except Exception as exc:  # noqa: BLE001
        out["MLflow"] = f"non mis à jour ({type(exc).__name__}) : rattrapé au prochain passage"
    try:
        from smartsupply.publish import publish
        out["Site public"] = publish(engine)["status"]
    except Exception as exc:  # noqa: BLE001
        out["Site public"] = f"non mis à jour ({type(exc).__name__})"
    return out


def describe(models: list[Model]) -> str:
    champion = current(models)
    lines = [f"{'Modèle':<18} {'Entraîné le':<17} {'Données au':<11} {'WAPE':>6} {'Naïve':>6}  État"]
    for m in models:
        state = ("EN SERVICE" if m is champion
                 else f"retiré le {m.withdrawn_at:%d/%m %H:%M} ({m.withdrawn_reason})" if m.withdrawn
                 else "ancien")
        lines.append(f"{m.run_id:<18} {m.trained_at:%d/%m/%Y %H:%M} {m.data_until!s:<11} "
                     f"{m.wape:>6.1%} {m.baseline_wape:>6.1%}  {state}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retour arrière du modèle en service.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--precedent", action="store_true", help="revenir au modèle retenu précédent")
    group.add_argument("--vers", metavar="RUN_ID", help="revenir à ce modèle")
    group.add_argument("--annuler", metavar="RUN_ID", help="annuler le retrait de ce modèle")
    parser.add_argument("--raison", default="retour arrière manuel", help="raison du retrait (gardée dans la base)")
    parser.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    args = parser.parse_args()

    engine = get_engine()
    models = promoted_models(engine)
    if not (args.precedent or args.vers or args.annuler):
        print(describe(models) if models else "Aucun modèle retenu pour l'instant.")
        return
    try:
        if args.annuler:
            question = f"Annuler le retrait de {args.annuler} ?"
            action = lambda: restore(engine, args.annuler)  # noqa: E731
        else:
            target, to_withdraw = plan(models, None if args.precedent else args.vers)
            names = ", ".join(m.run_id for m in to_withdraw)
            question = (f"Retirer {names} et remettre en service {target.run_id} "
                        f"(WAPE {target.wape:.1%}, données au {target.data_until}) ?")
            action = lambda: withdraw(engine, [m.run_id for m in to_withdraw], args.raison)  # noqa: E731
    except RollbackError as exc:
        raise SystemExit(f"Impossible : {exc}") from None
    if not args.oui and input(f"{question} [o/N] ").strip().lower() not in ("o", "oui", "y", "yes"):
        print("Rien n'a été changé.")
        return
    try:
        action()
    except RollbackError as exc:
        raise SystemExit(f"Impossible : {exc}") from None
    print("Fait.\n")
    print(describe(promoted_models(engine)))
    print()
    for target_name, status in propagate(engine).items():
        print(f"{target_name} : {status}")
    print("API locale : nouveau modèle chargé en moins de 5 minutes. "
          "Prochaine prévision (cette nuit) : avec le modèle en service.")


if __name__ == "__main__":
    main()
