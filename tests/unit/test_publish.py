"""Publication du site public : sans réseau (GitHub est simulé)."""
import json

import smartsupply.publish as pub


def _bundle(prevision="2026-11-20", run="R1", statut="ok", previsions=b"a,b\n1,2\n"):
    etat = {"prevision_du": prevision, "modele": {"run_id": run}, "cycle": {"statut": statut},
            "publie_le": "2026-11-21T02:40:00+00:00"}
    return {"etat.json": json.dumps(etat).encode(), "previsions.csv": previsions, "modele.joblib": b"x" * 10}


class FakeGitHub:
    def __init__(self, current=None):
        self.current, self.pushed = current, None

    def __call__(self, repo, token):
        return self

    def current_etat(self):
        return self.current

    def replace_branch(self, files, message):
        self.pushed = (files, message)
        return "abcdef1234567"


def _setup(monkeypatch, bundle, current):
    fake = FakeGitHub(current)
    monkeypatch.setattr(pub, "config", lambda: ("moi/smartsupply", "jeton"))
    monkeypatch.setattr(pub, "build_bundle", lambda engine: bundle)
    monkeypatch.setattr(pub, "GitHub", fake)
    return fake


def test_non_configuree(monkeypatch):
    monkeypatch.setattr(pub, "config", lambda: (None, None))
    assert pub.publish(engine=object())["status"].startswith("non configurée")


def test_premiere_publication(monkeypatch):
    fake = _setup(monkeypatch, _bundle(), current=None)
    result = pub.publish(engine=object())
    assert result["status"].startswith("publiées (3 fichiers, commit abcdef1")
    files, message = fake.pushed
    etat = json.loads(files["etat.json"])
    assert etat["tailles"] == {"modele.joblib": 10, "previsions.csv": 8}
    assert "2026-11-20" in message and "R1" in message


def test_rien_de_nouveau(monkeypatch):
    first = _setup(monkeypatch, _bundle(), current=None)
    pub.publish(engine=object())
    published = json.loads(first.pushed[0]["etat.json"])
    published["publie_le"] = "autre heure"          # l'heure seule ne justifie pas de republier
    fake = _setup(monkeypatch, _bundle(), current=published)
    assert pub.publish(engine=object())["status"] == "déjà à jour"
    assert fake.pushed is None
    assert pub.publish(engine=object(), force=True)["status"].startswith("publiées")


def test_nouveau_resultat(monkeypatch):
    first = _setup(monkeypatch, _bundle(), current=None)
    pub.publish(engine=object())
    published = json.loads(first.pushed[0]["etat.json"])
    for changed in (_bundle(prevision="2026-11-21"), _bundle(run="R2"), _bundle(statut="en retard"),
                    _bundle(previsions=b"a,b\n1,2\n3,4\n")):
        fake = _setup(monkeypatch, changed, current=published)
        assert pub.publish(engine=object())["status"].startswith("publiées"), changed
        assert fake.pushed is not None


def test_erreurs_lisibles(monkeypatch):
    import urllib.error

    class Refuse(FakeGitHub):
        def current_etat(self):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(pub, "config", lambda: ("moi/smartsupply", "jeton"))
    monkeypatch.setattr(pub, "build_bundle", lambda engine: _bundle())
    monkeypatch.setattr(pub, "GitHub", Refuse())
    assert pub.publish(engine=object())["status"] == "échec GitHub 403 : jeton sans droit d'écriture sur ce dépôt"

    def boom(engine):
        raise ConnectionError("base injoignable")
    monkeypatch.setattr(pub, "build_bundle", boom)
    assert pub.publish(engine=object())["status"].startswith("échec (ConnectionError")
