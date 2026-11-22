"""Démonstration du retour arrière automatique, sur la vraie base.

On met volontairement en service un modèle SABOTÉ, on le laisse publier la prévision du jour
(le pire cas), puis on lance le VRAI garde-fou (smartsupply.models.guard.guard, celui du cycle
de nuit). On montre qu'il :
    1. détecte le problème et le retire ;
    2. remet en service le modèle précédent ;
    3. refait la prévision du jour, identique à celle d'avant la panne.

Trois pannes, chacune attrapée par un contrôle différent :
    x4        bug d'unité : toutes les prévisions multipliées par 4      -> demande totale
    subtile   prévisions gonflées de 60 % (plausible à l'œil)            -> erreur sur 28 jours
    negative  bug de signe : un produit sur cinq prévu en négatif        -> valeurs négatives

À la fin de chaque panne, la base est remise EXACTEMENT dans son état d'origine (modèle de
démonstration effacé, prévision du jour d'origine remise). MLflow, le site public, Prometheus
et les alertes ne sont pas touchés. Ne pas lancer pendant le cycle de nuit (2h30).

    python -m smartsupply.models.demo_rollback                  # les trois pannes + rapport
    python -m smartsupply.models.demo_rollback --panne subtile
Rapport : docs/demo_retour_arriere.md (--rapport CHEMIN pour un autre fichier).
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from smartsupply.db.connection import get_engine

PREFIX = "DEMO_"
# ml.model_runs.data_until est UNIQUE : date fictive, qui ne gêne jamais le cycle de nuit.
# Le modèle de démonstration est effacé à la fin de chaque panne.
DEMO_DATA_UNTIL = date(2099, 12, 31)
DEFAULT_REPORT = Path("docs/demo_retour_arriere.md")

PANNES = {
    "x4": "Bug d'unité : toutes les prévisions multipliées par 4",
    "subtile": "Dérive discrète : prévisions gonflées de 60 %, plausibles à l'œil",
    "negative": "Bug de signe : un produit sur cinq prévu en négatif",
}


class SabotagedModel:
    """Le vrai modèle en service, avec une panne ajoutée à ses prédictions."""

    def __init__(self, base, panne: str):
        if panne not in PANNES:
            raise ValueError(f"panne inconnue : {panne}")
        self.base, self.panne = base, panne

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        pred = np.asarray(self.base.predict(X), dtype=float)
        if self.panne == "x4":
            return pred * 4
        if self.panne == "subtile":
            return pred * 1.6
        every_fifth = X["product_id"].to_numpy() % 5 == 0
        return np.where(every_fifth, -pred - 1, pred)


@dataclass
class Result:
    panne: str
    label: str
    bad_run: str
    good_run: str
    data_day: date
    published: str                                  # ce qu'a donné la prévision du modèle saboté
    guard: dict
    guard_seconds: float
    champion_after: str
    state_after: str                                # liste des modèles juste après le garde-fou
    original: pd.Series                             # prévision par produit (index product_id)
    bad: pd.Series
    restored: pd.Series
    timeline: list[tuple[float, str]] = field(default_factory=list)

    @property
    def caught(self) -> bool:
        return self.guard.get("status", "").startswith("retour arrière automatique")

    @property
    def identical(self) -> bool:
        return not self.original.empty and self.original.sort_index().equals(self.restored.sort_index())


# -----------------------------------------------------------------------------
# Base
# -----------------------------------------------------------------------------
def _day_forecasts(engine: Engine, day: date) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text("SELECT * FROM ml.forecasts WHERE as_of_date = :d"), conn, params={"d": day})


def _by_product(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype="int64")
    return df.set_index("product_id")["predicted_qty"].astype("int64").sort_index()


def _inject(engine: Engine, run_id: str, champion, panne: str) -> None:
    from smartsupply.models.nightly import dump_model
    blob = dump_model(SabotagedModel(champion.model, panne))
    with engine.begin() as conn:
        # Le modèle saboté annonce les MÊMES scores que le modèle en service : la panne est
        # arrivée après l'évaluation (empaquetage, mise en production...), invisible à l'entraînement.
        conn.execute(text("""
            INSERT INTO ml.model_runs (run_id, trained_at, model_name, data_until, eval_start, eval_end,
                n_train_rows, wape, mae, bias, baseline_wape, champion_run_id, champion_wape, promoted,
                decision, params, model_blob)
            SELECT :id, now(), 'démonstration', :du, eval_start, eval_end, n_train_rows, wape, mae, bias,
                   baseline_wape, run_id, wape, true, :decision, params, :blob
            FROM ml.model_runs WHERE run_id = :champion"""),
            {"id": run_id, "du": DEMO_DATA_UNTIL, "decision": f"démonstration : {PANNES[panne]}",
             "blob": blob, "champion": champion.run_id})


def _restore_database(engine: Engine, day: date, saved: pd.DataFrame) -> None:
    """Remet la prévision du jour d'origine et efface tout modèle de démonstration."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ml.forecasts WHERE as_of_date = :d"), {"d": day})
        if not saved.empty:
            saved.to_sql("forecasts", conn, schema="ml", if_exists="append", index=False)
        conn.execute(text("DELETE FROM ml.forecasts WHERE run_id LIKE :p"), {"p": PREFIX + "%"})
        conn.execute(text("DELETE FROM ml.model_runs WHERE run_id LIKE :p"), {"p": PREFIX + "%"})


def leftovers(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT run_id FROM ml.model_runs WHERE run_id LIKE :p"),
                                 {"p": PREFIX + "%"}).scalars())


def clean_leftovers(engine: Engine) -> list[str]:
    """Restes d'une démonstration interrompue : on les efface et on refait la prévision du jour si besoin."""
    from smartsupply.models.nightly import forecast_latest
    found = leftovers(engine)
    if found:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ml.forecasts WHERE run_id LIKE :p"), {"p": PREFIX + "%"})
            conn.execute(text("DELETE FROM ml.model_runs WHERE run_id LIKE :p"), {"p": PREFIX + "%"})
        forecast_latest(engine)
    return found


# -----------------------------------------------------------------------------
# Une panne
# -----------------------------------------------------------------------------
def run_scenario(engine: Engine, panne: str, say=print) -> Result:
    from smartsupply.models.guard import guard
    from smartsupply.models.nightly import current_champion, forecast_latest, latest_dates
    from smartsupply.models.rollback import describe, promoted_models

    champion = current_champion(engine)
    if champion is None:
        raise RuntimeError("aucun modèle en service : rien à démontrer")
    day, _ = latest_dates(engine)
    saved = _day_forecasts(engine, day)
    run_id = f"{PREFIX}{panne.upper()}"
    start = time.perf_counter()
    timeline: list[tuple[float, str]] = []

    def step(message: str) -> None:
        timeline.append((time.perf_counter() - start, message))
        say(f"  [{timeline[-1][0]:6.2f} s] {message}")

    try:
        _inject(engine, run_id, champion, panne)
        if current_champion(engine).run_id != run_id:
            raise RuntimeError("le modèle saboté n'a pas été mis en service")
        step(f"modèle saboté {run_id} mis en service à la place de {champion.run_id}")

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ml.forecasts WHERE as_of_date = :d"), {"d": day})
        try:
            out = forecast_latest(engine)
            published = f"publiée : {out['rows']} produits, {out['total_qty']} unités sur 7 jours"
        except IntegrityError:
            published = "refusée par la base (quantité négative interdite) : aucune prévision ce jour"
        bad = _by_product(_day_forecasts(engine, day))
        step(f"prévision du {day:%d/%m} par le modèle saboté : {published}")

        t0 = time.perf_counter()
        outcome = guard(engine)
        guard_seconds = time.perf_counter() - t0
        step(f"garde-fou : {outcome['status']}" + (f" — {outcome['reason']}" if "reason" in outcome else ""))
        state_after = describe(promoted_models(engine))

        follow = forecast_latest(engine)          # le cycle de nuit continue : prévision du jour
        restored = _by_product(_day_forecasts(engine, day))
        champion_after = current_champion(engine).run_id
        how = ("refaite par le garde-fou" if "refaite" in str(outcome.get("forecast", ""))
               else "faite par la suite du cycle" if follow.get("rows") else "déjà en place")
        step(f"service rétabli : modèle {champion_after} en service, prévision du {day:%d/%m} {how} "
             f"({int(restored.sum())} unités)")
    finally:
        _restore_database(engine, day, saved)
    step("base remise exactement dans son état d'origine")

    return Result(panne, PANNES[panne], run_id, champion.run_id, day, published, outcome, guard_seconds,
                  champion_after, state_after, _by_product(saved), bad, restored, timeline)


# -----------------------------------------------------------------------------
# Rapport
# -----------------------------------------------------------------------------
CHECKS = (("demande totale", "demande totale comparée à la méthode naïve"),
          ("des produits prévus", "écart par produit avec la méthode naïve"),
          ("derniers jours", "erreur sur les 28 derniers jours connus"),
          ("erreur récente", "erreur récente comparée à celle de l'entraînement"),
          ("négatives", "valeurs négatives"),
          ("manquantes", "valeurs manquantes"),
          ("vraies prévisions", "erreur des vraies prévisions"))


def check_name(reason: str | None) -> str:
    """Nom lisible du contrôle qui a attrapé la panne, d'après la raison donnée par le garde-fou."""
    if not reason:
        return "aucun"
    return next((name for key, name in CHECKS if key in reason), reason)


def _total(s: pd.Series) -> str:
    return "—" if s.empty else f"{int(s.sum()):,}".replace(",", " ")


def render_report(results: list[Result], names: dict[int, str] | None = None,
                  when: datetime | None = None) -> str:
    names = names or {}
    when = when or datetime.now()
    first = results[0]
    lines = [
        "# Démonstration : retour arrière automatique du modèle",
        "",
        f"*Générée le {when:%d/%m/%Y à %H:%M} par `python -m smartsupply.models.demo_rollback`, "
        f"sur la vraie base (données au {first.data_day:%d/%m/%Y}).*",
        "",
        "Chaque nuit, avant de prévoir, le garde-fou (`smartsupply.models.guard`) contrôle le modèle en "
        "service. Ici on **sabote volontairement** le modèle, on le laisse **publier sa prévision** "
        "(le pire cas), puis on lance **le vrai garde-fou**, le même code que la nuit.",
        "",
        "Le modèle saboté annonce les mêmes scores que le bon modèle : la panne arrive après "
        "l'évaluation, elle est invisible pour la règle de mise en service. Seul le garde-fou peut la voir.",
        "",
        "## Résultat",
        "",
        "| Panne | Contrôle qui l'attrape | Détecté | Retiré, précédent remis | Prévision rétablie | Durée du contrôle |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.label} | {check_name(r.guard.get('reason'))} | {'oui' if r.caught else '**non**'} | "
            f"{'oui → ' + r.champion_after if r.champion_after == r.good_run else '**non**'} | "
            f"{'identique à l’origine' if r.identical else '**différente**'} | {r.guard_seconds:.1f} s |")
    lines += [
        "",
        f"Modèle en service pendant toute la démonstration, en dehors des pannes : `{first.good_run}`. "
        "Après chaque panne, la base est remise exactement dans son état d'origine.",
    ]
    for r in results:
        lines += ["", f"## Panne « {r.panne} » — {r.label}", "", "Déroulé :", ""]
        lines += [f"- `{t:6.2f} s` {msg}" for t, msg in r.timeline]
        reason = r.guard.get("reason")
        if reason:
            lines += ["", f"Raison enregistrée dans la base : **automatique : {reason}**"]
        lines += ["", "Demande prévue sur 7 jours :", "",
                  "| | Avant la panne | Modèle saboté | Après le garde-fou |", "|---|---:|---:|---:|",
                  f"| **Total ({len(r.original)} produits)** | {_total(r.original)} | {_total(r.bad)} | "
                  f"{_total(r.restored)} |"]
        for pid in r.original.sort_values(ascending=False).index[:5]:
            bad = "—" if pid not in r.bad.index else int(r.bad[pid])
            restored = "—" if pid not in r.restored.index else int(r.restored[pid])
            lines.append(f"| {names.get(int(pid), f'produit {pid}')} | {int(r.original[pid])} | {bad} | {restored} |")
        lines += ["", "Modèles retenus juste après le garde-fou :", "", "```", r.state_after, "```"]
    lines += [
        "",
        "## Ce qui se passe en vrai, la nuit",
        "",
        "- Le garde-fou tourne **avant** la prévision : un modèle défectueux est en général retiré avant "
        "d'avoir publié quoi que ce soit. La démonstration montre le pire cas.",
        "- Le retrait déclenche l'alerte e-mail `RetourArriereAutomatique` ; MLflow (alias `champion`), "
        "le site public et l'API suivent le modèle remis en service dans la suite du cycle.",
        "- Si aucun modèle précédent ne passe les contrôles, le cycle s'arrête **avant** de prévoir "
        "(alerte `CycleEnEchec`) : mieux vaut pas de prévision qu'une prévision fausse.",
        "- Ici, rien de tout cela n'est déclenché : la démonstration remet la base dans son état d'origine "
        "en quelques secondes et ne touche ni MLflow, ni le site, ni Prometheus.",
        "",
        "Pour la refaire : `python -m smartsupply.models.demo_rollback` (hors du créneau de 2h30).",
        "",
    ]
    return "\n".join(lines)


def product_names(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return {int(pid): f"{sku} {name}" for pid, sku, name in
                conn.execute(text("SELECT product_id, sku, name FROM raw.products"))}


def main() -> None:
    parser = argparse.ArgumentParser(description="Démonstration du retour arrière automatique.")
    parser.add_argument("--panne", choices=[*PANNES, "toutes"], default="toutes")
    parser.add_argument("--rapport", type=Path, default=DEFAULT_REPORT, help="fichier du rapport (Markdown)")
    parser.add_argument("--sans-rapport", action="store_true")
    args = parser.parse_args()

    engine = get_engine()
    cleaned = clean_leftovers(engine)
    if cleaned:
        print(f"Restes d'une démonstration interrompue effacés : {', '.join(cleaned)}\n")

    results = []
    for panne in (list(PANNES) if args.panne == "toutes" else [args.panne]):
        print(f"Panne « {panne} » : {PANNES[panne]}")
        results.append(run_scenario(engine, panne))
        print()

    ok = all(r.caught and r.champion_after == r.good_run and r.identical for r in results)
    for r in results:
        print(f"{r.panne:<9} détectée={'oui' if r.caught else 'NON'}  "
              f"modèle remis={r.champion_after}  prévision identique={'oui' if r.identical else 'NON'}")
    if not args.sans_rapport:
        args.rapport.parent.mkdir(parents=True, exist_ok=True)
        args.rapport.write_text(render_report(results, product_names(engine)), encoding="utf-8")
        print(f"\nRapport : {args.rapport}")
    if leftovers(engine):
        raise SystemExit("ATTENTION : un modèle de démonstration est resté dans la base — relancer la commande.")
    if not ok:
        raise SystemExit("La démonstration n'a pas donné le résultat attendu (voir ci-dessus).")
    print("Démonstration réussie : base remise dans son état d'origine.")


if __name__ == "__main__":
    main()
