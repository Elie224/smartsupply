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
API_URL = "https://smartsupply-delta.vercel.app"
ALERT_ORDER = ["rupture probable", "à surveiller", "ok"]
ALERT_COLORS = {"rupture probable": "#c0392b", "à surveiller": "#e67e22", "ok": "#2e8b57"}
ALERT_ICONS = {"rupture probable": "🔴", "à surveiller": "🟠", "ok": "🟢"}
LEVEL_COLORS = {"stable": "#2e8b57", "à surveiller": "#e67e22", "forte": "#c0392b"}
BLUE, GREY = "#1f6feb", "#8b949e"

# Noms lisibles des variables du modèle (graphiques de dérive et d'importance).
LABELS = {
    "demand_lag_1": "Demande du jour",
    "demand_lag_7": "Demande il y a 6 jours",
    "demand_lag_14": "Demande il y a 13 jours",
    "demand_lag_28": "Demande il y a 27 jours",
    "demand_rolling_mean_7": "Demande moyenne 7 jours",
    "demand_rolling_mean_28": "Demande moyenne 28 jours",
    "demand_rolling_std_28": "Irrégularité de la demande (28 j)",
    "demand_rolling_mean_91": "Demande moyenne 91 jours",
    "demand_rolling_mean_364": "Demande moyenne sur un an",
    "demand_next7_last_year": "Même semaine l'an dernier",
    "lost_sales_rolling_7": "Ventes perdues (ruptures, 7 j)",
    "price_as_of": "Prix catalogue",
    "promotion_active": "Promotion en cours",
    "discount_pct": "Remise du jour",
    "promo_days_next_7": "Jours de promo à venir (7 j)",
    "promo_max_discount_next_7": "Plus forte remise à venir (7 j)",
    "weekday": "Jour de la semaine",
    "month": "Mois",
    "day_of_year": "Période de l'année",
    "is_weekend": "Week-end",
    "stock_as_of": "Stock disponible",
    "incoming_qty_known_as_of": "Livraisons attendues",
    "supplier_lead_time_known_as_of": "Délai du fournisseur",
    "category_season_index": "Saisonnalité de la catégorie",
    "category_trend_7_28": "Tendance de la catégorie",
    "category": "Catégorie du produit",
    "mean7_over_mean28": "Accélération récente (7 j / 28 j)",
    "cv_28": "Irrégularité relative (28 j)",
    "stock_cover_days": "Jours de stock",
    "incoming_cover_days": "Jours de livraisons attendues",
}


def label(feature: str) -> str:
    return LABELS.get(feature, feature)


st.set_page_config(page_title="SmartSupply AI — prévision de la demande", page_icon="📦", layout="wide",
                   menu_items={"About": "SmartSupply AI : prévision de la demande et MLOps de bout en bout, "
                                        "sur des données 100 % synthétiques."})
st.markdown("""
<style>
  .block-container {padding-top: 2rem; max-width: 1300px;}
  div[data-testid="stMetric"] {background: rgba(127,127,127,0.06); border: 1px solid rgba(127,127,127,0.18);
                               border-radius: 10px; padding: 12px 14px;}
  div[data-testid="stMetricLabel"] p {font-size: 0.85rem;}
  .pill {display:inline-block; padding: 2px 10px; margin: 0 6px 6px 0; border-radius: 999px;
         border: 1px solid rgba(127,127,127,0.35); font-size: 0.85rem; text-decoration: none;}
</style>""", unsafe_allow_html=True)


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


def chart(c: alt.Chart, height: int = 320) -> None:
    st.altair_chart(c.properties(height=height).configure_view(strokeWidth=0)
                    .configure_axis(labelFontSize=12, titleFontSize=12, grid=True, gridOpacity=0.25),
                    width="stretch")


# -----------------------------------------------------------------------------
# En-tête
# -----------------------------------------------------------------------------
cycle = etat.get("cycle") or {}
modele = etat.get("modele") or {}
volumes = etat.get("volumes") or {}
repo = _repo()
statut = cycle.get("statut", "inconnu")
icon = {"ok": "🟢", "en retard": "🟠", "échec": "🔴"}.get(statut, "⚪")

st.title("📦 SmartSupply AI")
st.markdown("Prévision de la demande à 7 jours et risque de rupture de stock pour un distributeur "
            "**fictif** — 50 produits, 3 entrepôts, environ 1 200 clients. Un système de bout en bout qui "
            "simule, apprend, prévoit et se surveille **tout seul, chaque nuit**. "
            "*Données 100 % synthétiques.*")
links = [f'<a class="pill" href="{API_URL}/docs" target="_blank">⚡ API de prévision</a>']
if repo:
    links.insert(0, f'<a class="pill" href="https://github.com/{repo}" target="_blank">💻 Code source</a>')
st.markdown("".join(links), unsafe_allow_html=True)
published = pd.to_datetime(etat["publie_le"]).tz_convert("Europe/Paris")
st.caption(f"{icon} **Cycle automatique : {statut}** · données jusqu'au {fr_date(etat.get('donnees_jusqu_au'))} "
           f"· publié le {published:%d/%m/%Y à %H:%M}")

tab_vue, tab_prev, tab_sim, tab_mlops, tab_about = st.tabs(
    ["📊 Vue d'ensemble", "📦 Prévisions & stocks", "🧪 Simulation « et si ? »", "🤖 Modèle & MLOps",
     "ℹ️ Comment ça marche"])

forecasts = csv("previsions.csv", ("as_of_date", "horizon_start", "horizon_end"))


# -----------------------------------------------------------------------------
# 1) Vue d'ensemble
# -----------------------------------------------------------------------------
with tab_vue:
    monthly = csv("ventes_mensuelles.csv", ("mois",))
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
        c[3].metric("Commandes (depuis le début)", fmt_int(volumes.get("commandes", 0)))
        c[4].metric("Clients", fmt_int(volumes.get("clients", 0)))

        left, right = st.columns([1.6, 1])
        with left:
            st.subheader("Chiffre d'affaires et marge, mois par mois")
            long = monthly.melt("mois", ["ca_eur", "marge_eur"], "série", "eur")
            long["série"] = long["série"].map({"ca_eur": "Chiffre d'affaires", "marge_eur": "Marge brute"})
            chart(alt.Chart(long).mark_area(opacity=0.35, line=True).encode(
                x=alt.X("mois:T", title=None, axis=alt.Axis(format="%m/%y")),
                y=alt.Y("eur:Q", title="€", stack=None),
                color=alt.Color("série:N", title=None, legend=alt.Legend(orient="top"),
                                scale=alt.Scale(range=[BLUE, "#2e8b57"])),
                tooltip=[alt.Tooltip("mois:T", format="%m/%Y", title="Mois"), alt.Tooltip("série:N", title=""),
                         alt.Tooltip("eur:Q", format=",.0f", title="€")]))
        with right:
            st.subheader("Taux de service")
            chart(alt.Chart(monthly).mark_line(point=True, color=BLUE).encode(
                x=alt.X("mois:T", title=None, axis=alt.Axis(format="%m/%y")),
                y=alt.Y("taux_service_pct:Q", title="% livré", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("mois:T", format="%m/%Y", title="Mois"),
                         alt.Tooltip("taux_service_pct:Q", title="Taux de service (%)", format=".1f")]))
        st.caption(f"Le mois en cours est partiel. Historique simulé depuis le {fr_date(volumes.get('debut'))} "
                   f"({fmt_int(volumes.get('jours_simules', 0))} jours), une journée de plus chaque nuit.")

    if not forecasts.empty:
        st.subheader("Demande prévue la semaine prochaine, par catégorie")
        by_cat = forecasts.groupby("category", as_index=False)[["predicted_qty", "predicted_revenue_eur"]].sum()
        chart(alt.Chart(by_cat).mark_bar(cornerRadiusEnd=4, color=BLUE).encode(
            x=alt.X("predicted_qty:Q", title="Unités prévues sur 7 jours"),
            y=alt.Y("category:N", sort="-x", title=None),
            tooltip=[alt.Tooltip("category:N", title="Catégorie"),
                     alt.Tooltip("predicted_qty:Q", title="Unités", format=",.0f"),
                     alt.Tooltip("predicted_revenue_eur:Q", title="CA prévu (€)", format=",.0f")]),
              height=40 + 32 * len(by_cat))


# -----------------------------------------------------------------------------
# 2) Prévisions & stocks
# -----------------------------------------------------------------------------
with tab_prev:
    if forecasts.empty:
        st.info("Pas de prévision publiée.")
    else:
        fc = forecasts
        start, end = fc["horizon_start"].min(), fc["horizon_end"].max()
        st.markdown(f"Demande prévue du **{start:%d/%m} au {end:%d/%m/%Y}** et stock projeté en fin de semaine, "
                    "livraisons fournisseurs attendues comprises.")
        counts = fc["forecast_alert"].value_counts()
        c = st.columns(4)
        c[0].metric("Demande prévue (7 j)", f"{fmt_int(fc['predicted_qty'].sum())} unités")
        c[1].metric("CA prévu (7 j)", fmt_eur(fc["predicted_revenue_eur"].sum()))
        c[2].metric("🔴 Rupture probable", int(counts.get("rupture probable", 0)))
        c[3].metric("🟠 À surveiller", int(counts.get("à surveiller", 0)))

        f1, f2, f3 = st.columns([1.2, 1, 1])
        search = f1.text_input("Rechercher un produit", placeholder="nom ou référence (ex. SKU-00010)")
        cats = f2.multiselect("Catégorie", sorted(fc["category"].unique()))
        alerts = f3.multiselect("Alerte", [a for a in ALERT_ORDER if a in counts.index])
        view = fc
        if search:
            mask = (view["product_name"].str.contains(search, case=False, regex=False)
                    | view["sku"].str.contains(search, case=False, regex=False))
            view = view[mask]
        if cats:
            view = view[view["category"].isin(cats)]
        if alerts:
            view = view[view["forecast_alert"].isin(alerts)]

        if view.empty:
            st.info("Aucun produit ne correspond.")
        else:
            st.subheader("Stock projeté en fin de semaine (les plus bas d'abord)")
            lowest = view.nsmallest(20, "projected_stock_end")
            chart(alt.Chart(lowest).mark_bar(cornerRadiusEnd=3).encode(
                x=alt.X("projected_stock_end:Q", title="Stock projeté (unités)"),
                y=alt.Y("product_name:N", sort="x", title=None),
                color=alt.Color("forecast_alert:N", title="Alerte",
                                scale=alt.Scale(domain=list(ALERT_COLORS), range=list(ALERT_COLORS.values())),
                                legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("product_name:N", title="Produit"),
                         alt.Tooltip("predicted_qty:Q", title="Demande prévue"),
                         alt.Tooltip("stock_as_of:Q", title="Stock actuel"),
                         alt.Tooltip("incoming_qty:Q", title="Livraisons attendues"),
                         alt.Tooltip("projected_stock_end:Q", title="Stock projeté")]),
                  height=40 + 22 * len(lowest))

            table = view.assign(
                _o=view["forecast_alert"].map({a: i for i, a in enumerate(ALERT_ORDER)}),
                alerte=view["forecast_alert"].map(lambda a: f"{ALERT_ICONS.get(a, '')} {a}"),
            ).sort_values(["_o", "projected_stock_end"])
            cols = ["alerte", "sku", "product_name", "category", "predicted_qty", "predicted_revenue_eur",
                    "stock_as_of", "incoming_qty", "projected_stock_end"]
            st.dataframe(
                table[cols], hide_index=True, width="stretch",
                column_config={
                    "alerte": "Alerte", "sku": "Réf.", "product_name": "Produit", "category": "Catégorie",
                    "predicted_qty": st.column_config.NumberColumn("Demande prévue", format="%d"),
                    "predicted_revenue_eur": st.column_config.NumberColumn("CA prévu (€)", format="%.0f"),
                    "stock_as_of": st.column_config.NumberColumn("Stock", format="%d"),
                    "incoming_qty": st.column_config.NumberColumn("Livraisons attendues", format="%d"),
                    "projected_stock_end": st.column_config.ProgressColumn(
                        "Stock projeté", format="%d", min_value=0,
                        max_value=int(max(1, fc["projected_stock_end"].max())))})
            st.download_button("⬇️ Télécharger ces prévisions (CSV)",
                               table[cols].to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"previsions_{start:%Y%m%d}.csv", mime="text/csv")
        st.caption("Règle d'alerte : stock + livraisons attendues inférieurs à la demande prévue → rupture probable ; "
                   "inférieurs à 1,5 × la demande prévue → à surveiller.")


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
        labels_ = {pid: f"{products.at[pid, 'name']} ({products.at[pid, 'sku']})" for pid in snaps["product_id"]}
        left, right = st.columns([1, 1.3])
        with left:
            pid = st.selectbox("Produit", list(labels_), format_func=labels_.get)
            base = snaps[snaps["product_id"] == pid].copy()
            row = base.iloc[0]
            price = float(row["price_as_of"])
            remise = st.slider("Remise (%)", 0, 30, round(100 * float(row["discount_pct"] or 0)), 5,
                               help="Les remises observées dans les données vont de 0 à 30 %.")
            jours = st.slider("Jours de promotion sur la semaine", 0, 7, int(row["promo_days_next_7"] or 0))
            prix = st.number_input("Prix de vente catalogue (€)", min_value=0.1, value=round(price, 2), step=0.5)
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
                ca_before = before * price * (1 - float(row["discount_pct"] or 0))
                ca_after = after * prix * (1 - remise / 100)
                c = st.columns(2)
                c[0].metric("CA actuel (7 j)", fmt_eur(ca_before))
                c[1].metric("CA avec vos hypothèses", fmt_eur(ca_after),
                            f"{(ca_after / ca_before - 1) * 100:+.0f} %".replace(".", ",") if ca_before else None)
                comp = pd.DataFrame({"scénario": ["Actuel", "Avec vos hypothèses"], "demande": [before, after]})
                chart(alt.Chart(comp).mark_bar(cornerRadiusEnd=4).encode(
                    x=alt.X("demande:Q", title="Demande prévue sur 7 jours (unités)"),
                    y=alt.Y("scénario:N", title=None, sort=None),
                    color=alt.Color("scénario:N", legend=None, scale=alt.Scale(range=[GREY, BLUE])),
                    tooltip=[alt.Tooltip("scénario:N"), alt.Tooltip("demande:Q", format=".0f")]), height=130)
                stock = float(row["stock_as_of"]) + float(row["incoming_qty_known_as_of"] or 0)
                if after > stock:
                    st.error(f"Stock disponible ({stock:.0f} unités, livraisons comprises) insuffisant : "
                             "rupture probable avec ces hypothèses.")
                elif after > stock / 1.5:
                    st.warning(f"Stock disponible : {stock:.0f} unités — à surveiller avec ces hypothèses.")
                else:
                    st.success(f"Stock disponible : {stock:.0f} unités — suffisant.")
            st.caption("Le CA estimé applique la remise sur toute la semaine : c'est un ordre de grandeur. "
                       f"La même simulation existe dans l'API : POST {API_URL}/simuler")


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
                    f"{100 * (modele['wape'] - modele['wape_methode_naive']):+.1f} pt pour le modèle".replace(".", ","),
                    delta_color="inverse")
        c[2].metric("Biais", fmt_pct(100 * modele["biais"]), help="> 0 : le modèle surestime")
        c[3].metric("Entraîné sur les données jusqu'au", fr_date(modele["donnees_jusqu_au"]))

    left, right = st.columns(2)
    with left:
        st.subheader("Ce qui compte pour le modèle")
        model, _ = load_model(SOURCE, etat["publie_le"])
        try:
            importance = model.feature_importance().head(12)
        except Exception:  # noqa: BLE001  (modèle absent ou d'un autre type)
            importance = pd.Series(dtype=float)
        if importance.empty:
            st.info("Importance des variables indisponible.")
        else:
            imp = pd.DataFrame({"variable": [label(f) for f in importance.index],
                                "part": importance.values / importance.sum()})
            chart(alt.Chart(imp).mark_bar(cornerRadiusEnd=3, color=BLUE).encode(
                x=alt.X("part:Q", title="Part des décisions des arbres (top 12)", axis=alt.Axis(format="%")),
                y=alt.Y("variable:N", sort="-x", title=None),
                tooltip=[alt.Tooltip("variable:N", title="Variable"),
                         alt.Tooltip("part:Q", title="Part", format=".1%")]))
            st.caption("Fréquence d'utilisation de chaque variable dans les arbres de LightGBM.")
    with right:
        st.subheader("Dérive des données")
        drift = csv("derive.csv")
        if drift.empty:
            st.info("Pas encore de mesure de dérive.")
        else:
            d = drift.head(12).assign(variable=lambda x: x["feature"].map(label))
            chart(alt.Chart(d).mark_bar(cornerRadiusEnd=3).encode(
                x=alt.X("psi:Q", title="PSI (0 = aucun changement)"),
                y=alt.Y("variable:N", sort="-x", title=None),
                color=alt.Color("level:N", title="Niveau", legend=alt.Legend(orient="top"),
                                scale=alt.Scale(domain=list(LEVEL_COLORS), range=list(LEVEL_COLORS.values()))),
                tooltip=[alt.Tooltip("variable:N", title="Variable"), alt.Tooltip("psi:Q", format=".3f", title="PSI"),
                         alt.Tooltip("level:N", title="Niveau")]))
            st.caption("Compare les 28 derniers jours à l'année qui a servi à l'entraînement : "
                       "< 0,10 stable · 0,10 à 0,25 à surveiller · > 0,25 forte (alerte).")

    st.subheader("Erreur réelle des prévisions passées")
    acc = csv("precision.csv", ("as_of_date",))
    if acc.empty:
        st.info("Les ventes réelles d'une prévision sont connues 7 jours plus tard : "
                "cette courbe se remplit au fil des nuits.")
    else:
        base_line = alt.Chart(acc).mark_line(point=True, color=BLUE).encode(
            x=alt.X("as_of_date:T", title="Prévision faite le", axis=alt.Axis(format="%d/%m")),
            y=alt.Y("wape_pct:Q", title="WAPE réelle (%)"),
            tooltip=[alt.Tooltip("as_of_date:T", title="Prévision du", format="%d/%m/%Y"),
                     alt.Tooltip("wape_pct:Q", title="WAPE (%)", format=".1f"),
                     alt.Tooltip("bias_pct:Q", title="Biais (%)", format=".1f"),
                     alt.Tooltip("predicted_qty:Q", title="Prévu"), alt.Tooltip("actual_qty:Q", title="Réel")])
        layers = [base_line]
        if modele:
            ref = pd.DataFrame({"y": [100 * modele["wape"]], "nom": ["erreur à l'entraînement"]})
            layers.append(alt.Chart(ref).mark_rule(strokeDash=[6, 4], color=GREY).encode(y="y:Q"))
        chart(alt.layer(*layers))

    st.subheader("Historique des entraînements")
    runs = csv("modeles.csv", ("trained_at", "data_until"))
    if not runs.empty:
        runs = runs.sort_values("trained_at", ascending=False)
        runs["état"] = runs.apply(
            lambda r: "🟢 en service" if r.get("is_current") else
            ("↩️ retiré" if pd.notna(r.get("withdrawn_at")) else ("retenu" if r["promoted"] else "refusé")), axis=1)
        st.dataframe(
            runs[["trained_at", "data_until", "wape_pct", "baseline_wape_pct", "bias_pct", "état", "decision"]],
            hide_index=True, width="stretch",
            column_config={
                "trained_at": st.column_config.DatetimeColumn("Entraîné le", format="DD/MM/YYYY HH:mm"),
                "data_until": st.column_config.DateColumn("Données jusqu'au", format="DD/MM/YYYY"),
                "wape_pct": st.column_config.NumberColumn("WAPE (%)", format="%.1f"),
                "baseline_wape_pct": st.column_config.NumberColumn("Naïve (%)", format="%.1f"),
                "bias_pct": st.column_config.NumberColumn("Biais (%)", format="%.1f"),
                "état": "État", "decision": "Décision"})
        st.caption("Un nouveau modèle n'est mis en service que s'il bat la méthode naïve, a un biais d'au plus "
                   "5 % et n'est pas moins bon que le modèle en place. Un modèle peut aussi être retiré "
                   "(retour arrière) : le précédent reprend sa place.")


# -----------------------------------------------------------------------------
# 5) Comment ça marche
# -----------------------------------------------------------------------------
with tab_about:
    st.markdown("""
**SmartSupply AI** est un projet d'apprentissage de bout en bout — data engineering, machine learning et
MLOps — sur les données d'un distributeur fictif. Tout tourne **seul, chaque nuit à 2 h 30** :
""")
    st.graphviz_chart("""
digraph {
  rankdir=LR; bgcolor="transparent";
  node [shape=box, style="rounded,filled", fillcolor="#eef4ff", color="#1f6feb", fontname="Helvetica", fontsize=11];
  edge [color="#8b949e"];
  sim [label="1. Simulation\\nd'une journée"]; qual [label="2. Contrôles\\nqualité"];
  feat [label="3. Variables\\ndu modèle"]; train [label="4. Réentraînement\\n+ règle de mise en service"];
  prev [label="5. Prévision\\nà 7 jours"]; surv [label="6. Surveillance\\n(dérive, erreur réelle)"];
  pub [label="7. Publication\\nsite · API · MLflow · Grafana", fillcolor="#e9f7ef", color="#2e8b57"];
  sim -> qual -> feat -> train -> prev -> surv -> pub;
}""")
    st.markdown("""
- **Données** : PostgreSQL ; journée simulée avec clients, promotions, ruptures et livraisons fournisseurs.
- **Modèle** : LightGBM, demande totale à 7 jours par produit. Il n'est **mis en service que s'il fait mieux**
  que la méthode naïve et que le modèle en place ; un **retour arrière** est possible.
- **MLOps** : orchestration Prefect, suivi MLflow, surveillance de la dérive, Prometheus et Grafana,
  alertes par e-mail, sauvegardes automatiques, tests et intégration continue (GitHub Actions).
- **Restitution** : ce site (Streamlit), une API de prévision (FastAPI) et un rapport Power BI.

Toutes les données sont **synthétiques** : aucune entreprise ni personne réelle.
""")
    if repo:
        st.markdown(f"💻 Code source : [github.com/{repo}](https://github.com/{repo})")
    st.markdown(f"⚡ API de prévision (même modèle, documentation interactive) : [{API_URL.removeprefix('https://')}/docs]({API_URL}/docs)")
    versions = etat.get("versions") or {}
    if versions:
        st.caption("Modèle produit avec : " + ", ".join(f"{k} {v}" for k, v in versions.items()))
