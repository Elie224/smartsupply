"""Tests unitaires du moteur de simulation (sans base de données)."""
from datetime import date, timedelta
from decimal import Decimal

from collections import Counter

from smartsupply.generator.clients import build_client_profile
from smartsupply.generator.demand import ProductProfile, expected_demand
from smartsupply.generator.simulator import (
    ID_KEYS,
    PurchaseOrder,
    SimulationState,
    simulate_day,
)

START = date(2024, 1, 1)
SHARES = {1: 0.6, 2: 0.4}
CLIENTS = [
    build_client_profile(i, "B2B" if i % 3 == 0 else "B2C", SHARES, 42) for i in range(1, 201)
]


def profile(product_id, base=20.0, lead=5, reliability=0.9):
    return ProductProfile(
        product_id=product_id, category="Boissons", supplier_id=1,
        unit_price=Decimal("10.00"), unit_cost=Decimal("6.00"),
        lead_time_days=lead, reliability=reliability,
        base_daily_demand=base, trend_per_year=0.0,
    )


def fresh_state(balances=None, in_transit=None):
    return SimulationState(
        day=START,
        balances=balances or {(1, 1): 5, (1, 2): 0, (2, 1): 200, (2, 2): 200},
        in_transit=in_transit or [],
        promotions=[],
        next_ids={**{k: 1 for k in ID_KEYS}, "client": len(CLIENTS) + 1},
    )


def run(days, state=None, profiles=None):
    state = state or fresh_state()
    profiles = profiles or [profile(1), profile(2)]
    results = []
    for _ in range(days):
        result, state = simulate_day(state, profiles, SHARES, CLIENTS, 42, START)
        results.append(result)
    return results, state


def test_deterministe():
    a, _ = run(10)
    b, _ = run(10)
    assert a == b


def test_stock_jamais_negatif_et_quantites_coherentes():
    results, _ = run(120)
    for r in results:
        assert all(q >= 0 for q in r.balances.values())
        for item in r.order_items:
            assert 0 <= item["fulfilled_qty"] <= item["requested_qty"]
            assert item["lost_sales_qty"] == item["requested_qty"] - item["fulfilled_qty"]


def test_rupture_tracee_comme_demande_perdue():
    # Produit 1 dans l'entrepôt 2 : stock 0 le premier jour -> tout est perdu.
    (first,), _ = run(1)
    items = [i for i in first.order_items if (i["product_id"], i["warehouse_id"]) == (1, 2)]
    assert items, "il devrait y avoir de la demande"
    assert sum(i["fulfilled_qty"] for i in items) == 0
    assert sum(i["lost_sales_qty"] for i in items) == sum(i["requested_qty"] for i in items)


def test_journal_coherent_avec_les_soldes():
    state = fresh_state()
    initial = dict(state.balances)
    results, final_state = run(60, state=state)
    total = dict(initial)
    for r in results:
        for m in r.stock_movements:
            key = (m["product_id"], m["warehouse_id"])
            total[key] = total.get(key, 0) + m["quantity_delta"]
    assert total == final_state.balances


def test_reception_seulement_a_echeance():
    def po(po_id, expected_in_days):
        return PurchaseOrder(
            po_id=po_id, supplier_id=1, warehouse_id=1,
            ordered_at=START - timedelta(days=10),
            expected_at=START + timedelta(days=expected_in_days),
            reliability=1.0,                    # toujours à l'heure (ou 1 j d'avance)
            items=[(po_id, 2, 50)],
        )
    state = fresh_state(in_transit=[po(1, -3), po(2, 30)])
    (result,), _ = run(1, state=state)
    assert result.received_po_ids == [1]       # la commande attendue dans 30 j n'arrive pas


def test_promotion_augmente_la_demande():
    p = profile(1)
    sans = expected_demand(p, 1.0, date(2024, 3, 5), 0.0, START)
    avec = expected_demand(p, 1.0, date(2024, 3, 5), 0.20, START)
    assert avec > sans * 1.5


def test_reapprovisionnement_declenche_quand_stock_bas():
    state = fresh_state(balances={(1, 1): 0, (1, 2): 0, (2, 1): 0, (2, 2): 0})
    (result,), _ = run(1, state=state)
    assert result.purchase_orders, "un stock vide doit déclencher un achat"


def test_une_commande_par_client_et_par_jour():
    results, _ = run(30)
    for r in results:
        clients_du_jour = [o["client_id"] for o in r.orders]
        assert len(clients_du_jour) == len(set(clients_du_jour))


def test_commande_servie_par_l_entrepot_du_client():
    profiles_by_id = {c.client_id: c for c in CLIENTS}
    results, _ = run(20)
    for r in results:
        client_of = {o["order_id"]: o["client_id"] for o in r.orders}
        for item in r.order_items:
            client = profiles_by_id.get(client_of[item["order_id"]])
            if client:   # les nouveaux clients du jour ne sont pas dans CLIENTS
                assert item["warehouse_id"] == client.warehouse_id


def test_clients_frequents_commandent_plus_souvent():
    results, _ = run(180)
    commandes = Counter(o["client_id"] for r in results for o in r.orders)
    tries = sorted(CLIENTS, key=lambda c: c.order_rate)
    dixieme = len(tries) // 10
    peu_actifs = sum(commandes[c.client_id] for c in tries[:dixieme])
    tres_actifs = sum(commandes[c.client_id] for c in tries[-dixieme:])
    assert tres_actifs > 5 * peu_actifs


def test_nouveaux_clients_rares_et_faker():
    results, _ = run(365)
    nouveaux = [c for r in results for c in r.clients]
    assert 30 <= len(nouveaux) <= 130            # ~0,2 par jour
    assert all(c["name"] and c["city"] for c in nouveaux)
    ids = [c["client_id"] for c in nouveaux]
    assert len(ids) == len(set(ids)) and min(ids) > len(CLIENTS)


def test_derniere_journee_terminee_est_hier():
    from datetime import datetime, timezone
    from smartsupply.generator.runner import last_complete_day
    assert last_complete_day(datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)) == date(2026, 11, 20)
    assert last_complete_day(datetime(2026, 11, 21, 23, 59, tzinfo=timezone.utc)) == date(2026, 11, 20)


def _service_rate(results, last_days):
    items = [i for r in results[-last_days:] for i in r.order_items]
    return sum(i["fulfilled_qty"] for i in items) / sum(i["requested_qty"] for i in items)


def test_acheteur_suit_une_demande_en_croissance():
    """+40 %/an : avec un niveau de commande figé au démarrage, le service s'effondrait.
    L'acheteur estime maintenant la demande sur les 28 derniers jours observés."""
    from dataclasses import replace

    growing = [replace(profile(1, base=20.0), trend_per_year=0.40),
               replace(profile(2, base=20.0), trend_per_year=0.40)]
    state = fresh_state(balances={(1, 1): 200, (1, 2): 150, (2, 1): 200, (2, 2): 150})
    results, final_state = run(540, state=state, profiles=growing)   # 18 mois
    assert _service_rate(results, 90) > 0.90
    # l'historique gardé en mémoire ne dépasse pas la fenêtre
    assert all(len(h) == 28 for h in final_state.recent_demand.values())


# --- Cycle de vie des clients -------------------------------------------------
def test_taux_de_depart_faible():
    from smartsupply.generator.clients import build_client_profile as bcp
    profiles = [bcp(i, "B2B" if i % 3 == 0 else "B2C", SHARES, 42,
                    created=date(2023, 6, 1), history_start=START) for i in range(1, 3001)]
    partis_en_un_an = sum(p.churn_at < START + timedelta(days=365) for p in profiles) / len(profiles)
    assert 0.03 < partis_en_un_an < 0.08          # ~5 % par an


def test_un_client_parti_ne_commande_plus_et_la_demande_baisse():
    from dataclasses import replace
    depart = START + timedelta(days=30)
    # la moitié des clients part au 30e jour
    clients = [replace(c, start=START, churn_at=depart if c.client_id % 2 else None) for c in CLIENTS]
    state, results = fresh_state(balances={k: 10_000 for k in [(1, 1), (1, 2), (2, 1), (2, 2)]}), []
    for _ in range(90):
        result, state = simulate_day(state, [profile(1), profile(2)], SHARES, clients, 42, START)
        results.append(result)
    partis = {c.client_id for c in clients if c.churn_at}
    apres = [r for r in results if r.day >= depart]
    clients_apres = {o["client_id"] for r in apres for o in r.orders}
    assert not clients_apres & partis

    def demande(rs):
        return sum(i["requested_qty"] for r in rs for i in r.order_items) / len(rs)
    avant = [r for r in results if r.day < depart]
    assert demande(apres) < 0.75 * demande(avant)   # la demande suit la base de clients


def test_nouveau_client_monte_en_puissance():
    from smartsupply.generator.clients import build_client_profile as bcp
    c = bcp(5000, "B2B", SHARES, 42, created=date(2025, 3, 1), history_start=START)
    assert c.is_new
    assert c.weight("Boissons", date(2025, 3, 1)) < 0.35 * c.weight("Boissons")
    assert c.weight("Boissons", date(2025, 6, 1)) == c.weight("Boissons") or not c.active(date(2025, 6, 1))
