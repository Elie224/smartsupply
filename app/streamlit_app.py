"""Site public SmartSupply AI (Streamlit).

Le site ne se connecte à AUCUNE base : il lit les résultats publiés chaque nuit par le
cycle dans la branche GitHub `donnees-publiques` (voir smartsupply.publish).

    streamlit run app/streamlit_app.py                              # lit GitHub
    $env:SMARTSUPPLY_DATA_DIR="sortie_locale"; streamlit run app/streamlit_app.py   # dossier local

Le dépôt lu est pris, dans l'ordre : secret Streamlit GITHUB_REPO, variable
d'environnement GITHUB_REPO, puis l'adresse « origin » du dépôt git cloné.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import altair as alt
import joblib
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))       # pour relire le modèle (classes smartsupply)

BRANCH = "donnees-publiques"
ALERT_ORDER = ["rupture probable", "à surveiller", "ok"]
ALERT_COLORS = {"rupture probable": "#c0392b", "à surveiller": "#e67e22", "ok": "#2e8b57"}

st.set_page_config(page_title="SmartSupply AI", page_icon="📦", layout="wide")


# -----------------------------------------------------------------------------
# Lecture des données publiées
# -----------------------------------------------------------------------------
def _repo() -> str | None:
    try:
        if "GITHUB_REPO" in st.secrets:
            return st.secrets["GITHUB_REPO"]
    except Exception:  # noqa: BLE001  (pas de fichier secrets en local)
        pass
    if os.getenv("GITHUB_REPO"):
        return os.environ["GITHUB_REPO"]
    git_config = ROOT / ".git" / "config"
    if git_config.exists():
        match = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s", git_config.read_text())
        if match:
            return match.group(1)
    return None


def _source() -> str:
    local = os.getenv("SMARTSUPPLY_DATA_DIR")
    if local:
        return str(Path(local).resolve())
    repo = _repo()
    if not repo:
        st.error("Dépôt GitHub inconnu : définir GITHUB_REPO (secret Streamlit ou variable "
                 "d'environnement), ou SMARTSUPPLY_DATA_DIR pour un dossier local.")
        st.stop()
    return f"https://raw.githubusercontent.com/{repo}/{BRANCH}"


@st.cache_data(ttl=1800, show_spinner=False)
def _read(source: str, name: str) -> bytes | None:
    if source.startswith("http"):
        try:
            with urllib.request.urlopen(f"{source}/{name}", timeout=30) as resp:
                return resp.read()
        except Exception:  # noqa: BLE001
            return None
    path = Path(source) / name
    return path.read_bytes() if path.exists() else None


def csv(name: str, dates: tuple[str, ...] = ()) -> pd.DataFrame:
    raw = _read(SOURCE, name)
    if not raw:
        return pd.DataFrame()
    df = pd.read_csv(io.BytesIO(raw))
    for col in dates:
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=col.endswith("_at")).dt.tz_localize(None) \
                if col.endswith("_at") else pd.to_datetime(df[col])
    return df


@st.cache_resource(show_spinner=False)
def load_model(source: str, published_at: str):
    """Relit le modèle publié. published_at sert seulement à recharger après une publication."""
    raw = _read(source, "modele.joblib")
    if not raw:
        return None, "modèle non publié"
    try:
        return joblib.load(io.BytesIO(raw)), None
    except Exception as exc:  # noqa: BLE001  (versions de bibliothèques différentes...)
        return None, f"{type(exc).__name__} : {exc}"


SOURCE = _source()
raw_etat = _read(SOURCE, "etat.json")
if not raw_etat:
    st.title("📦 SmartSupply AI")
    st.warning("Les résultats ne sont pas encore publiés (branche `donnees-publiques` absente). "
               "Ils apparaîtront après le prochain cycle de nuit.")
    st.stop()
etat = json.loads(raw_etat)


def fmt_int(x) -> str:
    return f"{x:,.0f}".replace(",", " ")


def fmt_eur(x) -> str:
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.2f} M€".replace(",", " ").replace(".", ",")
    return f"{fmt_int(x)} €"


def fmt_pct(x, digits=1) -> str:
    return f"{x:.{digits}f} %".replace(".", ",")


def fr_date(value) -> str:
    return pd.to_datetime(value).strftime("%d/%m/%Y") if value else "—"


# -----------------------------------------------------------------------------
# En-tête
# -----------------------------------------------------------------------------
st.title("📦 SmartSupply AI")
st.caption("Prévision de la demande et pilotage des stocks d'un distributeur fictif — "
           "**données 100 % synthétiques**, générées jour après jour sur le calendrier réel.")

cycle = etat.get("cycle") or {}
modele = etat.get("modele") or {}
statut = cycle.get("statut", "inconnu")
icon = {"ok": "🟢", "en retard": "🟠", "échec": "🔴"}.get(statut, "⚪")
st.markdown(f"{icon} **Cycle automatique : {statut}** · données jusqu'au "
            f"**{fr_date(etat.get('donnees_jusqu_au'))}** · prévision du "
            f"{fr_date(etat.get('prevision_du'))} · publié le "
            f"{pd.to_datetime(etat['publie_le']).tz_convert('Europe/Paris'):%d/%m/%Y à %H:%M}")

tab_vue, tab_prev, tab_sim, tab_mlops, tab_about = st.tabs(
    ["Vue d'ensemble", "Prévisions & stocks", "Simulation « et si ? »", "Modèle & MLOps", "À propos"])


# -----------------------------------------------------------------------------
# 1) Vue d'ensemble
# -----------------------------------------------------------------------------
with tab_vue:
    monthly = csv("ventes_mensuelles.csv", ("mois",))
    volumes = etat.get("volumes") or {}
    if not monthly.empty:
        full = monthly[monthly["jours"] >= 28]
        last12 = full.tail(12)
        prev12 = full.iloc[-24:-12] if len(full) >= 24 else pd.DataFrame()
        ca = last12["ca_eur"].sum()
        delta = (f"{(ca / prev12['ca_eur'].sum() - 1) * 100:+.1f} % sur un an".replace(".", ",")
                 if not prev12.empty else None)
        service = (last12["taux_service_pct"] * last12["jours"]).sum() / last12["jours"].sum()
        c = st.columns(5)
        c[0].metric("Chiffre d'affaires (12 mois)", fmt_eur(ca), delta)
        c[1].metric("Marge brute (12 mois)", fmt_eur(last12["marge_eur"].sum()),
                    f"{fmt_pct(100 * last12['marge_eur'].sum() / ca)} du CA", delta_color="off")
        c[2].metric("Taux de service", fmt_pct(service), help="Part des quantités demandées réellement livrées")
        c[3].metric("Commandes (total)", fmt_int(volumes.get("commandes", 0)))
        c[4].metric("Clients", fmt_int(volumes.get("clients", 0)))

        st.subheader("Chiffre d'affaires et marge par mois")
        long = monthly.melt("mois", ["ca_eur", "marge_eur"], "série", "eur")
        long["série"] = long["série"].map({"ca_eur": "Chiffre d'affaires", "marge_eur": "Marge brute"})
        st.altair_chart(
            alt.Chart(long).mark_line(point=True).encode(
                x=alt.X("mois:T", title=None, axis=alt.Axis(format="%m/%Y")),
                y=alt.Y("eur:Q", title="€"),
                color=alt.Color("série:N", title=None, legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("mois:T", format="%m/%Y", title="Mois"), "série:N",
                         alt.Tooltip("eur:Q", format=",.0f", title="€")]),
            width="stretch")
        st.caption("Le mois en cours est partiel. Les données commencent le "
                   f"{fr_date(volumes.get('debut'))} ({fmt_int(volumes.get('jours_simules', 0))} jours simulés).")


# -----------------------------------------------------------------------------
# 2) Prévisions & stocks
# -----------------------------------------------------------------------------
with tab_prev:
    fc = csv("previsions.csv", ("as_of_date", "horizon_start", "horizon_end"))
    if fc.empty:
        st.info("Pas de prévision publiée.")
    else:
        start, end = fc["horizon_start"].min(), fc["horizon_end"].max()
        st.markdown(f"Demande prévue du **{start:%d/%m} au {end:%d/%m/%Y}** (7 jours) et stock "
                    "projeté à la fin de la semaine, compte tenu des livraisons fournisseurs attendues.")
        counts = fc["forecast_alert"].value_counts()
        c = st.columns(4)
        c[0].metric("Demande prévue (7 j)", f"{fmt_int(fc['predicted_qty'].sum())} unités")
        c[1].metric("CA prévu (7 j)", fmt_eur(fc["predicted_revenue_eur"].sum()))
        c[2].metric("🔴 Rupture probable", int(counts.get("rupture probable", 0)))
        c[3].metric("🟠 À surveiller", int(counts.get("à surveiller", 0)))

        f1, f2 = st.columns(2)
        cats = f1.multiselect("Catégorie", sorted(fc["category"].unique()))
        alerts = f2.multiselect("Alerte", [a for a in ALERT_ORDER if a in counts.index])
        view = fc
        if cats:
            view = view[view["category"].isin(cats)]
        if alerts:
            view = view[view["forecast_alert"].isin(alerts)]

        st.altair_chart(
            alt.Chart(view.nlargest(20, "predicted_qty")).mark_bar().encode(
                x=alt.X("predicted_qty:Q", title="Demande prévue sur 7 jours (unités)"),
                y=alt.Y("product_name:N", sort="-x", title=None),
                color=alt.Color("forecast_alert:N", title="Alerte",
                                scale=alt.Scale(domain=list(ALERT_COLORS), range=list(ALERT_COLORS.values()))),
                tooltip=[alt.Tooltip("product_name:N", title="Produit"),
                         alt.Tooltip("predicted_qty:Q", title="Demande prévue"),
                         alt.Tooltip("stock_as_of:Q", title="Stock actuel"),
                         alt.Tooltip("incoming_qty:Q", title="Livraisons attendues"),
                         alt.Tooltip("projected_stock_end:Q", title="Stock projeté")]
            ).properties(title="Les 20 produits les plus demandés", height=480),
            width="stretch")

        table = view.assign(_o=view["forecast_alert"].map({a: i for i, a in enumerate(ALERT_ORDER)})) \
            .sort_values(["_o", "projected_stock_end"])
        st.dataframe(
            table[["sku", "product_name", "category", "predicted_qty", "predicted_revenue_eur",
                   "stock_as_of", "incoming_qty", "projected_stock_end", "forecast_alert"]],
            hide_index=True, width="stretch",
            column_config={
                "sku": "Réf.", "product_name": "Produit", "category": "Catégorie",
                "predicted_qty": st.column_config.NumberColumn("Demande prévue", format="%d"),
                "predicted_revenue_eur": st.column_config.NumberColumn("CA prévu (€)", format="%.0f"),
                "stock_as_of": st.column_config.NumberColumn("Stock", format="%d"),
                "incoming_qty": st.column_config.NumberColumn("Livraisons attendues", format="%d"),
                "projected_stock_end": st.column_config.NumberColumn("Stock projeté", format="%d"),
                "forecast_alert": "Alerte"})
        st.caption("Règle d'alerte : stock disponible inférieur à la demande prévue → rupture probable ; "
                   "inférieur à 1,5 × la demande prévue → à surveiller.")


# -----------------------------------------------------------------------------
# 3) Simulation
# -----------------------------------------------------------------------------
with tab_sim:
    st.markdown("Que deviendrait la demande d'un produit sur les 7 prochains jours avec une promotion "
                "ou un autre prix ? Le **modèle en service** est rejoué sur les données réelles du "
                "produit, avec vos hypothèses. Rien n'est enregistré.")
    model, error = load_model(SOURCE, etat["publie_le"])
    snaps_raw = _read(SOURCE, "features_derniere_journee.parquet")
    if model is None or not snaps_raw:
        st.warning(f"Simulation indisponible : {error or 'données du modèle non publiées'}.")
    else:
        snaps = pd.read_parquet(io.BytesIO(snaps_raw))
        products = csv("produits.csv").set_index("product_id")
        snaps = snaps[snaps["product_id"].isin(products.index)]
        labels = {pid: f"{products.at[pid, 'name']} ({products.at[pid, 'sku']})" for pid in snaps["product_id"]}
        left, right = st.columns([1, 1.3])
        with left:
            pid = st.selectbox("Produit", list(labels), format_func=labels.get)
            base = snaps[snaps["product_id"] == pid].copy()
            row = base.iloc[0]
            price = float(row["price_as_of"])
            remise = st.slider("Remise (%)", 0, 30, int(round(100 * float(row["discount_pct"] or 0))), 5)
            jours = st.slider("Jours de promotion sur la semaine", 0, 7, int(row["promo_days_next_7"] or 0))
            prix = st.number_input("Prix de vente (€)", min_value=0.1, value=round(price, 2), step=0.5)
        changed = base.copy()
        changed["discount_pct"] = remise / 100
        changed["promotion_active"] = remise > 0
        changed["promo_days_next_7"] = jours
        changed["promo_max_discount_next_7"] = remise / 100 if jours else 0.0
        changed["price_as_of"] = prix
        try:
            before, after = (float(v) for v in model.predict(pd.concat([base, changed], ignore_index=True)))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Le modèle n'a pas pu calculer : {exc}")
        else:
            with right:
                c = st.columns(2)
                c[0].metric("Demande prévue actuelle (7 j)", f"{before:.0f} unités")
                c[1].metric("Avec vos hypothèses", f"{after:.0f} unités",
                            f"{(after / before - 1) * 100:+.0f} %".replace(".", ",") if before else None)
                c = st.columns(2)
                c[0].metric("CA actuel (7 j)", fmt_eur(before * price * (1 - float(row["discount_pct"] or 0))))
                c[1].metric("CA avec vos hypothèses", fmt_eur(after * prix * (1 - remise / 100)))
                stock = float(row["stock_as_of"]) + float(row["incoming_qty_known_as_of"] or 0)
                if after > stock:
                    st.error(f"Stock disponible ({stock:.0f} unités, livraisons comprises) insuffisant : "
                             "rupture probable avec ces hypothèses.")
                elif after > stock / 1.5:
                    st.warning(f"Stock disponible : {stock:.0f} unités — à surveiller avec ces hypothèses.")
                else:
                    st.success(f"Stock disponible : {stock:.0f} unités — suffisant.")
            st.caption("Le CA estimé applique la remise sur toute la semaine : c'est un ordre de grandeur.")


# -----------------------------------------------------------------------------
# 4) Modèle & MLOps
# -----------------------------------------------------------------------------
with tab_mlops:
    if modele:
        c = st.columns(4)
        c[0].metric("Erreur du modèle (WAPE)", fmt_pct(100 * modele["wape"]),
                    help="Erreur absolue totale / demande totale, sur les 8 dernières semaines connues "
                         "au moment de l'entraînement")
        c[1].metric("Méthode naïve (moyenne 28 j)", fmt_pct(100 * modele["wape_methode_naive"]),
                    f"{100 * (modele['wape'] - modele['wape_methode_naive']):+.1f} pt".replace(".", ","),
                    delta_color="inverse")
        c[2].metric("Biais", fmt_pct(100 * modele["biais"]), help="> 0 : le modèle surestime")
        c[3].metric("Entraîné sur les données jusqu'au", fr_date(modele["donnees_jusqu_au"]))

    acc = csv("precision.csv", ("as_of_date",))
    surv = csv("surveillance.csv", ("as_of_date",))
    st.subheader("Erreur réelle des prévisions passées")
    if acc.empty:
        st.info("Les ventes réelles d'une prévision sont connues 7 jours plus tard : "
                "cette courbe se remplit au fil des nuits.")
    else:
        st.altair_chart(
            alt.Chart(acc).mark_line(point=True).encode(
                x=alt.X("as_of_date:T", title="Prévision faite le"),
                y=alt.Y("wape_pct:Q", title="WAPE réelle (%)"),
                tooltip=[alt.Tooltip("as_of_date:T", title="Prévision du"),
                         alt.Tooltip("wape_pct:Q", title="WAPE (%)", format=".1f"),
                         alt.Tooltip("bias_pct:Q", title="Biais (%)", format=".1f"),
                         alt.Tooltip("predicted_qty:Q", title="Prévu"),
                         alt.Tooltip("actual_qty:Q", title="Réel")]),
            width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("Dérive des données (PSI)")
        drift = csv("derive.csv")
        if drift.empty:
            st.info("Pas encore de mesure de dérive.")
        else:
            st.altair_chart(
                alt.Chart(drift.head(12)).mark_bar().encode(
                    x=alt.X("psi:Q", title="PSI"), y=alt.Y("feature:N", sort="-x", title=None),
                    color=alt.Color("level:N", title="Niveau", scale=alt.Scale(
                        domain=["stable", "à surveiller", "forte"], range=["#2e8b57", "#e67e22", "#c0392b"])),
                    tooltip=["feature", alt.Tooltip("psi:Q", format=".3f"), "level"]),
                width="stretch")
            st.caption("Compare les 28 derniers jours à l'année qui a servi à l'entraînement. "
                       "< 0,10 stable · 0,10–0,25 à surveiller · > 0,25 forte.")
    with right:
        st.subheader("Historique des entraînements")
        runs = csv("modeles.csv", ("trained_at", "data_until"))
        if not runs.empty:
            st.dataframe(
                runs.sort_values("trained_at", ascending=False)[
                    ["trained_at", "data_until", "wape_pct", "baseline_wape_pct", "bias_pct", "decision"]],
                hide_index=True, width="stretch",
                column_config={
                    "trained_at": st.column_config.DatetimeColumn("Entraîné le", format="DD/MM/YYYY HH:mm"),
                    "data_until": st.column_config.DateColumn("Données jusqu'au", format="DD/MM/YYYY"),
                    "wape_pct": st.column_config.NumberColumn("WAPE (%)", format="%.1f"),
                    "baseline_wape_pct": st.column_config.NumberColumn("Naïve (%)", format="%.1f"),
                    "bias_pct": st.column_config.NumberColumn("Biais (%)", format="%.1f"),
                    "decision": "Décision"})
            st.caption("Un nouveau modèle n'est mis en service que s'il bat la méthode naïve, "
                       "a un biais d'au plus 5 % et n'est pas moins bon que le modèle en place.")
    if not surv.empty and len(surv) > 1:
        st.subheader("Surveillance jour par jour")
        st.line_chart(surv.set_index("as_of_date")[["max_psi"]].rename(columns={"max_psi": "PSI maximal"}))


# -----------------------------------------------------------------------------
# 5) À propos
# -----------------------------------------------------------------------------
with tab_about:
    repo = _repo()
    st.markdown("""
**SmartSupply AI** est un projet d'apprentissage de bout en bout : data engineering,
machine learning et MLOps, sur les données d'un distributeur fictif.

**Chaque nuit, automatiquement** (orchestration Prefect, dans Docker) :

1. une journée de commandes est simulée (clients, promotions, ruptures, livraisons fournisseurs) ;
2. la qualité des données est contrôlée ;
3. les variables du modèle sont recalculées ;
4. un modèle LightGBM est réentraîné, et **mis en service seulement s'il est assez bon** ;
5. la demande des 7 prochains jours est prévue, produit par produit ;
6. le modèle est surveillé (dérive des données, erreur réelle) ;
7. tout est tracé dans MLflow et suivi dans Prometheus / Grafana, avec alertes par e-mail ;
8. les résultats sont publiés ici.

**Technologies** : PostgreSQL · Python (pandas, scikit-learn, LightGBM) · Prefect · MLflow ·
FastAPI · Prometheus · Grafana · Alertmanager · Docker · Power BI · Streamlit.

Toutes les données sont **synthétiques** : aucune entreprise ni personne réelle.
""")
    if repo:
        st.markdown(f"Code source : [github.com/{repo}](https://github.com/{repo})")
    st.markdown("API de prévision en ligne (même modèle, documentation interactive) : "
                "[smartsupply-delta.vercel.app/docs](https://smartsupply-delta.vercel.app/docs)")
    versions = etat.get("versions") or {}
    if versions:
        st.caption("Modèle produit avec : " + ", ".join(f"{k} {v}" for k, v in versions.items()))
