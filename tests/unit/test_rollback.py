"""Retour arrière : choix des modèles à retirer (fonction pure, sans base)."""
from datetime import date, datetime, timezone

import pytest

from smartsupply.models.rollback import Model, RollbackError, current, plan


def m(run_id, day, withdrawn=False):
    return Model(run_id, datetime(2026, 11, day, 2, 30, tzinfo=timezone.utc), date(2026, 11, day - 8),
                 0.12, 0.19, datetime(2026, 11, 25, tzinfo=timezone.utc) if withdrawn else None,
                 "test" if withdrawn else None)


MODELS = [m("R4", 24), m("R3", 23), m("R2", 22, withdrawn=True), m("R1", 21)]   # du plus récent au plus ancien


def test_le_modele_en_service_ignore_les_retires():
    assert current(MODELS).run_id == "R4"
    assert current([m("R2", 22, withdrawn=True), m("R1", 21)]).run_id == "R1"


def test_revenir_au_precedent_saute_les_retires():
    target, out = plan(MODELS)
    assert target.run_id == "R3" and [x.run_id for x in out] == ["R4"]
    target, out = plan(MODELS[1:])                      # sans R4 : R3 en service, R2 retiré
    assert target.run_id == "R1" and [x.run_id for x in out] == ["R3"]


def test_revenir_a_un_modele_precis_retire_tous_les_plus_recents():
    target, out = plan(MODELS, "R1")
    assert target.run_id == "R1" and [x.run_id for x in out] == ["R4", "R3"]


@pytest.mark.parametrize("models, target, message", [
    ([], None, "aucun modèle"),
    ([m("R1", 21)], None, "pas de modèle précédent"),
    (MODELS, "R9", "n'est pas un modèle retenu"),
    (MODELS, "R2", "est retiré"),
    (MODELS, "R4", "déjà le modèle en service"),
])
def test_cas_impossibles(models, target, message):
    with pytest.raises(RollbackError, match=message):
        plan(models, target)
