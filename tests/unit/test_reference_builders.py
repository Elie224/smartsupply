"""Tests unitaires : pas besoin de base de données."""
import numpy as np
from faker import Faker

from smartsupply.generator.reference_data import (
    HISTORY_START,
    build_clients,
    build_products,
    build_suppliers,
)


def _generators(seed=42):
    fake = Faker("fr_FR")
    fake.seed_instance(seed)
    return fake, np.random.default_rng(seed)


def test_meme_seed_memes_donnees():
    fake1, rng1 = _generators()
    fake2, rng2 = _generators()
    assert build_suppliers(10, fake1, rng1) == build_suppliers(10, fake2, rng2)


def test_produits_coherents():
    fake, rng = _generators()
    products = build_products(200, 10, fake, rng)
    for p in products:
        assert p["unit_cost"] > 0
        assert p["unit_price"] >= p["unit_cost"]
        assert 1 <= p["supplier_id"] <= 10
        assert p["created_at"] < HISTORY_START
    assert len({p["sku"] for p in products}) == 200  # SKU uniques


def test_clients_segments_valides():
    fake, rng = _generators()
    clients = build_clients(200, fake, rng)
    assert {c["segment"] for c in clients} <= {"B2B", "B2C"}
    assert all(c["created_at"] < HISTORY_START for c in clients)
