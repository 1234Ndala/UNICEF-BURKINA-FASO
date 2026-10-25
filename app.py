"""Tableau de bord de suivi de la collecte des prix (UNICEF Burkina Faso).

Lecture en continu des soumissions KoboToolbox (API v2), suivi par enquêteur,
vague, localité, famille d'articles et jour, contrôles qualité et export final.
"""

import io
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

st.set_page_config(page_title="Suivi de la collecte des prix", layout="wide")

TZ = "Africa/Ouagadougou"
BASE_DIR = Path(__file__).parent
NR = "Non renseigné"

LABEL_LISTS = {
    "enqueteur": "enqueteurs",
    "enqueteur_2": "enqueteurs",
    "vague": "vagues",
    "ville": "villes",
    "quartier": "quartiers",
    "famille": "familles",
    "article": "articles",
    "conformite": "conformite",
    "resultat_visite": "resultats",
    "disponibilite": "dispo",
    "mode_collecte": "modes",
    "nature_source": "sources",
    "base_prix": "base_prix",
}

VISIT_COLS = [
    "_id", "_uuid", "_submission_time", "_submitted_by", "id_visite", "today", "start", "end",
    "enqueteur", "enqueteur_2", "vague", "ville", "ville_autre", "quartier", "quartier_autre",
    "adresse", "gps", "mode_collecte", "nom_fournisseur", "collecte_possible", "motif_impossible",
    "resultat_visite", "resultat_precision", "nb_articles", "commentaire_visite",
    "type_fournisseur", "validation",
]
ITEM_COLS = [
    "_id", "famille", "article", "disponibilite", "quantite_stock", "conformite", "conformite_prec",
    "marque", "etat", "origine", "unite_vente", "unite_base", "nb_unites", "qte_prix", "devise",
    "base_prix", "prix_ht", "prix_ttc", "prix_retenu", "base_retenue", "prix_comparable",
    "livraison", "niveau_prix", "conditions_paiement", "nature_source", "justificatif", "date_obs", "commentaire",
]
VALIDATION_FR = {"Approved": "Approuvé", "Not Approved": "Non approuvé", "On Hold": "En attente"}
NUM_ITEM = ["quantite_stock", "nb_unites", "qte_prix", "prix_ht", "prix_ttc", "prix_retenu", "prix_comparable"]


# ------------------------------------------------------------------ Configuration
def get_conf(key, default=""):
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


def check_password():
    pwd_equipe = get_conf("APP_PASSWORD")
    pwd_client = get_conf("APP_PASSWORD_CLIENT")
    if not pwd_equipe and not pwd_client:
        st.session_state["role"] = "equipe"
    if st.session_state.get("role"):
        return True
    st.title("Accès restreint")
    saisie = st.text_input("Mot de passe", type="password")
    if saisie:
        if pwd_equipe and saisie == pwd_equipe:
            st.session_state["role"] = "equipe"
            st.rerun()
        elif pwd_client and saisie == pwd_client:
            st.session_state["role"] = "client"
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    return False


if not check_password():
    st.stop()

INTERNE = st.session_state.get("role") == "equipe"

px.defaults.template = "simple_white"
px.defaults.color_discrete_sequence = ["#1f4e79", "#8a96a3", "#c0792b", "#4f8f6f", "#b9c2cb", "#6b4c7a"]
st.markdown(
    "<style>#MainMenu{visibility:hidden;} footer{visibility:hidden;} [data-testid='stToolbar']{display:none;}"
    " h1{font-size:1.9rem;} h2,h3{font-weight:600;}</style>",
    unsafe_allow_html=True,
)

# Les identifiants Kobo ne sont jamais envoyés au navigateur lorsqu'ils sont définis dans les Secrets.
server = get_conf("KOBO_SERVER", "https://kf.kobotoolbox.org")
token = get_conf("KOBO_TOKEN")
asset_uid = get_conf("KOBO_ASSET_UID")
if INTERNE and not (token and asset_uid):
    st.sidebar.header("Connexion KoboToolbox")
    server = st.sidebar.text_input("Serveur", server)
    token = st.sidebar.text_input("Jeton API", token, type="password")
    asset_uid = st.sidebar.text_input("Identifiant du formulaire", asset_uid)

st.sidebar.header("Actualisation")
if INTERNE:
    interval = st.sidebar.selectbox("Actualisation automatique", ["Désactivée", "1 minute", "5 minutes", "15 minutes"], index=2)
else:
    interval = "5 minutes"
if interval != "Désactivée" and st_autorefresh is not None:
    st_autorefresh(interval=int(interval.split()[0]) * 60 * 1000, key="auto_refresh")
if st.sidebar.button("Actualiser maintenant"):
    st.cache_data.clear()
    st.rerun()


# ------------------------------------------------------------------ Chargement
def first_label(v):
    if isinstance(v, list):
        v = next((x for x in v if x), "")
    return "" if v is None else str(v)


@st.cache_data(ttl=600)
def load_choices(server_url, api_token, uid):
    """Libellés lus directement dans la définition du formulaire KoboToolbox."""
    base = server_url.strip().rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    c = None
    try:
        r = requests.get(f"{base}/api/v2/assets/{uid}/", params={"format": "json"},
                         headers={"Authorization": f"Token {api_token}"}, timeout=60)
        r.raise_for_status()
        rows = r.json().get("content", {}).get("choices", [])
        c = pd.DataFrame([{
            "list_name": ch.get("list_name", ""), "name": str(ch.get("name", "")),
            "label": first_label(ch.get("label")), "statut": str(ch.get("statut") or ""),
            "famille": str(ch.get("famille") or ""),
        } for ch in rows])
    except Exception:
        path = BASE_DIR / "choices.csv"  # secours facultatif
        if path.exists():
            c = pd.read_csv(path, dtype=str).fillna("")
    if c is None or c.empty:
        return {}, pd.DataFrame(columns=["name", "label", "statut", "famille"])
    labels = {ln: dict(zip(g["name"], g["label"])) for ln, g in c.groupby("list_name", sort=False)}
    labels["familles"] = {k: v.split(" (")[0] for k, v in labels.get("familles", {}).items()}
    art = c[c["list_name"] == "articles"][["name", "label", "statut", "famille"]].reset_index(drop=True)
    return labels, art


@st.cache_data(ttl=60, show_spinner="Chargement des données...")
def fetch_data(server_url, api_token, uid):
    base = server_url.strip().rstrip("/")
    if base and not base.startswith("http"):
        base = "https://" + base
    url = f"{base}/api/v2/assets/{uid}/data/"
    params = {"format": "json", "limit": 1000}
    headers = {"Authorization": f"Token {api_token}"}
    rows = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=90)
        r.raise_for_status()
        payload = r.json()
        rows.extend(payload.get("results", []))
        url = payload.get("next")
        params = None
    return rows, datetime.now(ZoneInfo(TZ)).strftime("%d/%m/%Y %H:%M")


def last(key):
    return key.split("/")[-1]


def flat(rec):
    return {last(k): v for k, v in rec.items() if not isinstance(v, (list, dict))}


def find_repeat(rec):
    for k, v in rec.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and any(last(x) == "famille" for x in v[0]):
            return v
    return []


def map_label(value, mapping, with_code=False):
    if pd.isna(value):
        return value
    lab = mapping.get(value)
    if lab is None:
        return value
    return f"{value} {lab}" if with_code else lab


def apply_labels(df, labels):
    for col, ln in LABEL_LISTS.items():
        if col in df.columns:
            df[col + "_code"] = df[col]
            m = labels.get(ln, {})
            df[col] = df[col].map(lambda v, m=m, ln=ln: map_label(v, m, with_code=(ln == "enqueteurs")))
    return df


def build_tables(records, labels):
    visits, items = [], []
    for rec in records:
        v = flat(rec)
        vs = rec.get("_validation_status") or {}
        v["validation"] = VALIDATION_FR.get(vs.get("label"), "Non vérifié") if vs.get("label") else "Non vérifié"
        visits.append(v)
        for obs in find_repeat(rec):
            row = flat(obs)
            row["_id"] = rec.get("_id")
            items.append(row)

    dv = pd.DataFrame(visits).reindex(columns=VISIT_COLS)
    di = pd.DataFrame(items).reindex(columns=ITEM_COLS)

    # Dates
    for c in ("start", "end"):
        dv[c] = pd.to_datetime(dv[c], utc=True, errors="coerce").dt.tz_convert(TZ)
    dv["_submission_time"] = pd.to_datetime(dv["_submission_time"], utc=True, errors="coerce").dt.tz_convert(TZ)
    dv["jour_visite"] = pd.to_datetime(dv["today"], errors="coerce").dt.normalize()
    dv["jour_reception"] = dv["_submission_time"].dt.tz_localize(None).dt.normalize()
    dv["jour_visite"] = dv["jour_visite"].fillna(dv["jour_reception"])
    dv["duree_min"] = (dv["end"] - dv["start"]).dt.total_seconds() / 60

    # Coordonnées GPS (latitude longitude altitude précision)
    gps = dv["gps"].astype("string").str.split(" ", expand=True)
    dv["latitude"] = pd.to_numeric(gps[0], errors="coerce") if 0 in gps else pd.NA
    dv["longitude"] = pd.to_numeric(gps[1], errors="coerce") if 1 in gps else pd.NA

    dv["nb_articles"] = pd.to_numeric(dv["nb_articles"], errors="coerce")
    dv["aboutie"] = dv["resultat_visite"].eq("aboutie")

    # Articles
    for c in NUM_ITEM:
        di[c] = pd.to_numeric(di[c], errors="coerce")
    di["releve"] = di["disponibilite"].isin(["stock", "commande"])
    di["different"] = di["conformite"].eq("different")
    di["retenue"] = di["releve"] & ~di["different"]
    di["valide"] = di["retenue"] & (di["prix_retenu"] > 0)
    di["article_code"] = di["article"]
    di["famille_code"] = di["famille"]

    keep = ["_id", "id_visite", "enqueteur", "vague", "ville", "quartier", "nom_fournisseur",
            "mode_collecte", "jour_visite", "jour_reception", "type_fournisseur", "validation"]
    di = di.merge(dv[keep], on="_id", how="left")

    dv = apply_labels(dv, labels)
    di = apply_labels(di, labels)
    return dv, di


# ------------------------------------------------------------------ Synthèses
def pct(num, den):
    den = den.astype(float)
    return (num / den.where(den > 0) * 100).round(1)


def summarize(visits, items, key, nomenclature=None):
    v, i = visits.copy(), items.copy()
    if key in v.columns:
        v[key] = v[key].fillna(NR)
    i[key] = i[key].fillna(NR)

    parts = []
    if key in v.columns:
        parts.append(v.groupby(key).agg(**{"Visites": ("_id", "size"), "Visites abouties": ("aboutie", "sum")}))
    if len(i):
        g = i.groupby(key).agg(**{
            "Articles saisis": ("_id", "size"),
            "Prix relevés": ("releve", "sum"),
            "Observations retenues": ("retenue", "sum"),
            "Produits différents": ("different", "sum"),
        })
        dist = i[i["releve"]].groupby(key)["article_code"].nunique().rename("Articles distincts")
        parts += [g, dist]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, axis=1).fillna(0)
    for c in out.columns:
        out[c] = out[c].astype(int)
    if "Visites" in out and "Visites abouties" in out:
        out["Taux d'aboutissement (%)"] = pct(out["Visites abouties"], out["Visites"])
    if "Prix relevés" in out:
        out["Taux de produits différents (%)"] = pct(out["Produits différents"], out["Prix relevés"])
    if nomenclature is not None and "Articles distincts" in out:
        out["Articles à couvrir"] = out.index.map(nomenclature).fillna(0).astype(int)
        out["Couverture (%)"] = pct(out["Articles distincts"], out["Articles à couvrir"])
    return out.reset_index().rename(columns={key: key.capitalize() if key != "jour" else "Jour"})


def coverage_table(valides, pret, labels, seuil):
    """Une ligne par article de la nomenclature, avec le nombre de prix valides et le statut."""
    cnt = valides.groupby("article_code").size()
    art = pret.rename(columns={"name": "article_code", "label": "Article"}).copy()
    art["Famille"] = art["famille"].map(labels.get("familles", {})).fillna(art["famille"])
    art["Prix valides"] = art["article_code"].map(cnt).fillna(0).astype(int)
    art["Statut"] = art["Prix valides"].map(lambda n: statut_label(n, seuil))
    return art[["Famille", "article_code", "Article", "Prix valides", "Statut"]]


def statut_label(n, seuil):
    if n == 0:
        return "Non couvert"
    if n >= seuil:
        return f"{seuil} prix ou plus"
    return "1 prix" if seuil == 2 else f"1 à {seuil - 1} prix"


def coverage_by_famille(art, seuil):
    g = art.groupby("Famille").agg(**{
        "Articles": ("article_code", "size"),
        "Articles couverts": ("Prix valides", lambda s: int((s >= seuil).sum())),
    })
    g["Couverture (%)"] = pct(g["Articles couverts"], g["Articles"])
    return g.reset_index()


def to_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            out = df.copy()
            for c in out.columns:
                if isinstance(out[c].dtype, pd.DatetimeTZDtype):
                    out[c] = out[c].dt.tz_localize(None)
            out.to_excel(w, index=False, sheet_name=name[:31])
            ws = w.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for idx, col in enumerate(out.columns, start=1):
                width = max([len(str(col))] + [len(str(x)) for x in out[col].head(200).tolist()]) + 2
                ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = min(width, 45)
    return buf.getvalue()


# ------------------------------------------------------------------ Page
st.title("Suivi de la collecte des prix")
st.caption("UNICEF Burkina Faso")

if not (server and token and asset_uid):
    st.info("Renseigner le serveur, le jeton API et l'identifiant du formulaire dans la barre latérale.")
    st.stop()

try:
    records, fetched_at = fetch_data(server, token, asset_uid)
except requests.HTTPError as e:
    code = e.response.status_code if e.response is not None else "?"
    st.error(f"Connexion à KoboToolbox impossible (code {code}). Vérifier le serveur, le jeton et l'identifiant du formulaire.")
    st.stop()
except Exception as e:
    st.error(f"Erreur lors du chargement : {e}")
    st.stop()

if not records:
    st.warning("Aucune soumission reçue pour le moment.")
    st.stop()

labels, art_ref = load_choices(server, token, asset_uid)
visits_all, items_all = build_tables(records, labels)

# Nomenclature : articles sélectionnables (statut « pret ») par famille
pret = art_ref[art_ref["statut"] == "pret"]
nomenc_total = len(pret)
nomenc_fam = pret.groupby("famille").size().rename(lambda k: labels.get("familles", {}).get(k, k)).to_dict()

# ------------------------------------------------------------------ Filtres
st.sidebar.header("Filtres")
ref = st.sidebar.radio("Date de référence", ["Date de la visite", "Date de réception"], help="Date saisie sur le terminal ou date d'arrivée de la fiche sur le serveur.") if INTERNE else "Date de la visite"
ref_col = "jour_visite" if ref == "Date de la visite" else "jour_reception"
visits_all["jour"] = visits_all[ref_col]
items_all["jour"] = items_all[ref_col]

dmin, dmax = visits_all["jour"].min().date(), visits_all["jour"].max().date()
periode = st.sidebar.date_input("Période", (dmin, dmax), min_value=dmin, max_value=dmax)

vague_order = [v for v in labels.get("vagues", {}).values() if v in set(visits_all["vague"].dropna())]
sel = {}
sel["vague"] = st.sidebar.multiselect("Vague", vague_order or sorted(visits_all["vague"].dropna().unique()))
sel["ville"] = st.sidebar.multiselect("Ville", sorted(visits_all["ville"].dropna().unique()))
sel["enqueteur"] = st.sidebar.multiselect("Enquêteur", sorted(visits_all["enqueteur"].dropna().unique())) if INTERNE else []

visits, items = visits_all.copy(), items_all.copy()
if isinstance(periode, tuple) and len(periode) == 2:
    d0, d1 = pd.Timestamp(periode[0]), pd.Timestamp(periode[1])
    visits = visits[(visits["jour"] >= d0) & (visits["jour"] <= d1)]
    items = items[(items["jour"] >= d0) & (items["jour"] <= d1)]
for k, vals in sel.items():
    if vals:
        visits = visits[visits[k].isin(vals)]
        items = items[items[k].isin(vals)]

if visits.empty:
    st.warning("Aucune donnée pour les filtres sélectionnés.")
    st.stop()

# ------------------------------------------------------------------ Indicateurs
releves = items[items["releve"]]
n_dist = releves["article_code"].nunique()
n_diff = int(items["different"].sum())
now = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
aujourdhui = int((releves["jour"] == now).sum())

st.caption(f"Les filtres de la barre latérale s'appliquent à tous les indicateurs. Données chargées le {fetched_at}.")

seuil, objectif_saisi = 2, 0
if INTERNE:
    with st.sidebar.expander("Paramètres de suivi"):
        seuil = int(st.number_input("Prix valides minimum par article", 1, 10, 2))
        objectif_saisi = int(st.number_input("Objectif de prix valides (0 : articles à couvrir x minimum)", 0, 100000, 0))
objectif = objectif_saisi or max(nomenc_total, 1) * seuil


def norm_name(series):
    return series.fillna("").astype(str).str.lower().str.strip()


valides = items[items["valide"]].assign(_four=lambda d: norm_name(d["nom_fournisseur"]))
vis_n = visits.assign(_four=norm_name(visits["nom_fournisseur"]))
art_cov = coverage_table(valides, pret, labels, seuil)
n_couverts = int((art_cov["Prix valides"] >= seuil).sum())
n_four = vis_n.loc[vis_n["_four"] != "", "_four"].nunique()
villes_prev = [c for c in labels.get("villes", {}) if c != "autre"]
n_villes_prev = len(villes_prev) or 11
villes_couv = valides.loc[valides["ville_code"].isin(villes_prev), "ville_code"].nunique()

# ------------------------------------------------------------------ Indicateurs
releves = items[items["releve"]]
n_dist = releves["article_code"].nunique()
n_diff = int(items["different"].sum())
now = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
aujourdhui = int((releves["jour"] == now).sum())

n_un_prix = int((art_cov["Prix valides"] >= 1).sum())
k = st.columns(6)
k[0].metric("Articles avec au moins 1 prix", f"{n_un_prix} sur {nomenc_total}",
            f"{n_un_prix / nomenc_total * 100:.1f} % de la nomenclature" if nomenc_total else None, delta_color="off", delta_arrow="off")
k[1].metric("Articles couverts", f"{n_couverts} sur {nomenc_total}",
            f"{n_couverts / nomenc_total * 100:.1f} % ({seuil} prix valides ou plus)" if nomenc_total else None, delta_color="off", delta_arrow="off")
k[2].metric("Prix valides collectés", f"{len(valides)} sur {objectif}", f"{len(valides) / objectif * 100:.1f} % de l'objectif", delta_color="off", delta_arrow="off")
k[3].metric("Fournisseurs distincts contactés", n_four, f"{vis_n.loc[vis_n['aboutie'], '_four'].nunique()} avec prix relevés", delta_color="off", delta_arrow="off")
k[4].metric("Villes couvertes", f"{villes_couv} sur {n_villes_prev}", "au moins un prix valide", delta_color="off", delta_arrow="off")
k[5].metric("Dernière mise à jour", fetched_at.split(" ")[1], f"Dernière fiche reçue le {visits['_submission_time'].max().strftime('%d/%m à %H:%M')}", delta_color="off", delta_arrow="off")

CLIENT_TABS = ["Avancement", "Fournisseurs et villes", "Fiabilité", "Prix de référence", "Méthodologie"]
INTERNE_TABS = ["Enquêteurs", "Vagues et localités", "Familles et articles", "Suivi journalier",
                "Tableau croisé", "Contrôle qualité", "Données"]
noms = CLIENT_TABS[:-1] + INTERNE_TABS + ["Méthodologie"] if INTERNE else CLIENT_TABS
T = dict(zip(noms, st.tabs(noms)))

compo_colors = {"Observations retenues": "#2a6f97", "Produits différents": "#e07a1f", "Non relevés": "#b8bec6"}


def composition_chart(df, key_name, title, horizontal=True):
    d = df.copy()
    d["Non relevés"] = d["Articles saisis"] - d["Prix relevés"]
    long = d.melt(id_vars=key_name, value_vars=["Observations retenues", "Produits différents", "Non relevés"],
                  var_name="Statut", value_name="Articles")
    if horizontal:
        fig = px.bar(long, y=key_name, x="Articles", color="Statut", orientation="h", title=title,
                     color_discrete_map=compo_colors)
        fig.update_yaxes(categoryorder="total ascending")
    else:
        fig = px.bar(long, x=key_name, y="Articles", color="Statut", title=title, color_discrete_map=compo_colors)
    fig.update_layout(legend_title_text="", margin=dict(t=50, b=10))
    return fig


def share_chart(series, title):
    s_ = series.dropna()
    if s_.empty:
        return None
    d_ = (s_.value_counts(normalize=True) * 100).round(1).rename_axis("Modalité").reset_index(name="Part (%)")
    fig_ = px.bar(d_, y="Modalité", x="Part (%)", orientation="h", title=title, text="Part (%)", range_x=[0, 105])
    fig_.update_yaxes(categoryorder="total ascending")
    fig_.update_layout(margin=dict(t=50, b=10))
    return fig_


def fmt_pct(x):
    return "n.d." if pd.isna(x) else f"{x:.1f} %"


# ---- Avancement
with T["Avancement"]:
    s_part = "1 prix" if seuil == 2 else f"1 à {seuil - 1} prix"
    s_full = f"{seuil} prix ou plus"
    statut_ordre = ["Non couvert", s_part, s_full] if seuil > 1 else ["Non couvert", s_full]
    statut_couleur = {"Non couvert": "#b8bec6", s_part: "#e07a1f", s_full: "#2a6f97"}

    st.subheader("Couverture par famille d'articles")
    cf = coverage_by_famille(art_cov, seuil)
    if cf.empty:
        st.info("La nomenclature n'a pas pu être lue dans le formulaire.")
    else:
        cf["Libellé"] = cf["Famille"] + " (" + cf["Articles"].astype(str) + " articles)"
        cf["Texte"] = cf["Articles couverts"].astype(str) + " sur " + cf["Articles"].astype(str)
        fig = px.bar(cf, y="Libellé", x="Couverture (%)", orientation="h", text="Texte", range_x=[0, 115],
                     title=f"Articles couverts (au moins {seuil} prix valides), en %")
        fig.update_yaxes(categoryorder="total ascending")
        fig.update_traces(textposition="outside", marker_color="#2a6f97")
        fig.update_layout(margin=dict(t=50, b=10), height=max(360, 34 * len(cf)))
        st.plotly_chart(fig, width="stretch")

        st.subheader("Statut des articles par famille")
        stat = art_cov.groupby(["Famille", "Statut"]).size().rename("Articles").reset_index()
        fig = px.bar(stat, y="Famille", x="Articles", color="Statut", orientation="h",
                     category_orders={"Statut": statut_ordre}, color_discrete_map=statut_couleur)
        fig.update_yaxes(categoryorder="total ascending")
        fig.update_layout(legend_title_text="", margin=dict(t=20, b=10), height=max(360, 34 * len(cf)))
        st.plotly_chart(fig, width="stretch")

        st.subheader("Statut par article")
        f1, f2 = st.columns(2)
        fam_f = f1.multiselect("Famille", sorted(art_cov["Famille"].unique()), key="av_fam")
        sta_f = f2.multiselect("Statut", statut_ordre, default=["Non couvert"], key="av_sta")
        vue_art = art_cov
        if fam_f:
            vue_art = vue_art[vue_art["Famille"].isin(fam_f)]
        if sta_f:
            vue_art = vue_art[vue_art["Statut"].isin(sta_f)]
        st.write(f"{len(vue_art)} article(s).")
        st.dataframe(vue_art.drop(columns="article_code").sort_values(["Famille", "Prix valides"]),
                     hide_index=True, width="stretch", height=360)

# ---- Fournisseurs et villes
with T["Fournisseurs et villes"]:
    st.subheader("Prix valides par ville")
    villes_liste = list(labels.get("villes", {}).values()) or sorted(visits["ville"].dropna().unique())
    vis_v = vis_n.groupby("ville").agg(**{"Visites": ("_id", "size"),
                                          "Fournisseurs contactés": ("_four", lambda x: x[x != ""].nunique())})
    prix_v = valides.groupby("ville").size().rename("Prix valides")
    tab_v = pd.DataFrame({"Ville": villes_liste}).merge(vis_v, left_on="Ville", right_index=True, how="left") \
        .merge(prix_v, left_on="Ville", right_index=True, how="left").fillna(0)
    for c_ in ("Visites", "Fournisseurs contactés", "Prix valides"):
        tab_v[c_] = tab_v[c_].astype(int)
    st.dataframe(tab_v, hide_index=True, width="stretch")
    fig = px.bar(tab_v, y="Ville", x="Prix valides", orientation="h", text="Prix valides", title="Prix valides par ville")
    fig.update_yaxes(categoryorder="total ascending")
    fig.update_layout(margin=dict(t=50, b=10), height=max(320, 32 * len(tab_v)))
    st.plotly_chart(fig, width="stretch")

    g1, g2 = st.columns(2)
    with g1:
        st.subheader("Par type de fournisseur")
        tv = valides[["_four", "type_fournisseur"]].copy()
        tv["t"] = tv["type_fournisseur"].fillna("").str.split()
        tv = tv.explode("t")
        tv = tv[tv["t"].notna() & (tv["t"] != "")]
        if len(tv):
            tv["Type"] = tv["t"].map(lambda x: labels.get("type_fourn", {}).get(x, x))
            tt = tv.groupby("Type").agg(**{"Prix valides": ("t", "size"),
                                           "Fournisseurs": ("_four", lambda x: x[x != ""].nunique())}).reset_index()
            fig = px.bar(tt, y="Type", x="Prix valides", orientation="h", text="Prix valides")
            fig.update_yaxes(categoryorder="total ascending")
            fig.update_layout(margin=dict(t=20, b=10))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(tt, hide_index=True, width="stretch")
            st.caption("Un fournisseur déclaré sous plusieurs types est compté dans chacun.")
        else:
            st.info("Aucun type de fournisseur renseigné.")
    with g2:
        st.subheader("Par canal de collecte")
        vc = vis_n.groupby("mode_collecte").size().rename("Visites")
        pc = valides.groupby("mode_collecte").size().rename("Prix valides")
        canal = pd.concat([vc, pc], axis=1).fillna(0).astype(int).rename_axis("Canal").reset_index()
        if len(canal):
            fig = px.bar(canal, y="Canal", x="Prix valides", orientation="h", text="Prix valides")
            fig.update_yaxes(categoryorder="total ascending")
            fig.update_layout(margin=dict(t=20, b=10))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(canal, hide_index=True, width="stretch")

    st.subheader("Carte des points de vente")
    if st.checkbox("Afficher la carte", value=False, key="carte_client"):
        geo_c = visits.dropna(subset=["latitude", "longitude"])
        if len(geo_c):
            st.map(geo_c, latitude="latitude", longitude="longitude")
        else:
            st.info("Aucune coordonnée GPS disponible.")

# ---- Fiabilité
with T["Fiabilité"]:
    st.caption("Les parts sont calculées sur les prix valides, sauf la conformité et la base de prix, calculées sur l'ensemble des prix relevés.")
    rel_ = items[items["releve"]]
    just = valides["justificatif"].fillna("").astype(str).str.strip().ne("")
    conf = rel_["conformite_code"].isin(["identique", "equivalent"])
    ver = valides["validation"].eq("Approuvé")
    m = st.columns(5)
    m[0].metric("Prix avec justificatif", fmt_pct(just.mean() * 100 if len(just) else float("nan")))
    m[1].metric("Prix à source déclarée", fmt_pct(valides["nature_source"].notna().mean() * 100 if len(valides) else float("nan")))
    m[2].metric("Produits conformes", fmt_pct(conf.mean() * 100 if len(conf) else float("nan")), "identiques ou équivalents", delta_color="off", delta_arrow="off")
    m[3].metric("Prix en base HT et TTC", fmt_pct((rel_["base_prix_code"] == "les_deux").mean() * 100 if len(rel_) else float("nan")))
    m[4].metric("Prix contrôlés (approuvés)", fmt_pct(ver.mean() * 100 if len(ver) else float("nan")), "statut de validation Kobo", delta_color="off", delta_arrow="off")

    h1, h2 = st.columns(2)
    for col_, ser, titre in ((h1, valides["nature_source"], "Nature de la source du prix"),
                             (h2, rel_["conformite"], "Conformité à la nomenclature")):
        fig = share_chart(ser, titre)
        if fig is not None:
            col_.plotly_chart(fig, width="stretch")
    h3, h4 = st.columns(2)
    for col_, ser, titre in ((h3, rel_["base_prix"], "Base du prix communiqué"),
                             (h4, valides["validation"], "Contrôle par le superviseur")):
        fig = share_chart(ser, titre)
        if fig is not None:
            col_.plotly_chart(fig, width="stretch")
    st.caption("Le contrôle par le superviseur utilise le statut de validation de KoboToolbox (Approuvé, Non approuvé, En attente). Les fiches non encore examinées sont « Non vérifié ».")

# ---- Prix de référence
with T["Prix de référence"]:
    st.caption("Indicateurs provisoires, non validés, présentés par famille sans détail par article. Prix par unité de base, en XOF, prix valides uniquement.")
    xo = valides[(valides["devise"] == "xof") & (valides["prix_comparable"] > 0)]
    if len(xo):
        ref_f = xo.groupby("famille").agg(**{
            "Prix valides": ("prix_comparable", "size"),
            "Minimum": ("prix_comparable", "min"),
            "Médiane": ("prix_comparable", "median"),
            "Maximum": ("prix_comparable", "max"),
        })
        pa = xo.groupby(["famille", "article_code"])["prix_comparable"].agg(["count", "min", "max"])
        pa = pa[pa["count"] >= 2]
        pa["ecart"] = (pa["max"] / pa["min"] - 1) * 100
        ref_f["Articles avec 2 prix ou plus"] = pa.groupby("famille").size()
        ref_f["Écart médian entre prix d'un même article (%)"] = pa.groupby("famille")["ecart"].median().round(1)
        ref_f = ref_f.fillna({"Articles avec 2 prix ou plus": 0}).reset_index().rename(columns={"famille": "Famille"})
        ref_f["Articles avec 2 prix ou plus"] = ref_f["Articles avec 2 prix ou plus"].astype(int)
        for c_ in ("Minimum", "Médiane", "Maximum"):
            ref_f[c_] = ref_f[c_].round(0).astype(int)
        st.dataframe(ref_f, hide_index=True, width="stretch")
        st.caption("Une famille regroupe des articles de natures et d'unités différentes : le minimum, la médiane et le maximum ne sont pas des prix de référence d'un article. "
                   "L'écart médian entre prix d'un même article est l'indicateur le plus lisible de la dispersion des prix collectés.")
    else:
        st.info("Aucun prix valide en XOF pour le moment.")

# ---- Méthodologie
with T["Méthodologie"]:
    vagues_txt = ", ".join(labels.get("vagues", {}).values()) or "voir le formulaire"
    st.markdown(f"""
### Prix valide
Un prix est valide lorsque l'article est disponible en stock ou sur commande, que le fournisseur a communiqué un prix, et que le produit proposé est jugé **identique** ou **équivalent** à la désignation de la nomenclature. Les produits jugés **différents** sont conservés dans la base pour la traçabilité mais ne sont pas comptés.

### Article couvert
Un article de la nomenclature est couvert lorsqu'il compte au moins **{seuil} prix valides**. La nomenclature comprend **{nomenc_total} articles** sélectionnables (statut « prêt »). Trois niveaux sont suivis : non couvert, couverture partielle, couvert.

### Objectif de collecte
L'objectif est fixé à **{objectif} prix valides**, soit {seuil} prix par article de la nomenclature.

### Base de prix
Le prix retenu est le prix hors taxes (HT), sauf lorsque le fournisseur ne communique que le prix toutes taxes comprises (TTC). Le prix par unité de base est calculé par le formulaire à partir du prix retenu, de la quantité du prix et du nombre d'unités de base par unité de vente.

### Périmètre de la collecte
Vagues prévues : {vagues_txt}. Villes prévues : {n_villes_prev}. Un fournisseur est comptabilisé une seule fois, quel que soit le nombre de visites ou d'articles, sur la base du nom saisi : deux orthographes différentes comptent pour deux fournisseurs.

### Fiabilité
La part de prix avec justificatif correspond aux prix accompagnés d'une photo (devis, catalogue ou étiquette). La source déclarée est la nature de la preuve du prix (affichage, oral, devis, catalogue). Le contrôle par le superviseur repose sur le statut de validation de KoboToolbox.

### Limites
Le tableau de bord ne regroupe pas automatiquement les points de vente d'un même groupe : le formulaire demande de les signaler, mais plusieurs prix d'un même groupe sont comptés séparément. Les données sont rechargées automatiquement toutes les 5 minutes par défaut (réglable dans la barre latérale) ou à la demande avec le bouton « Actualiser maintenant ».
""")


if not INTERNE:
    st.stop()

# ---- Enquêteurs
with T["Enquêteurs"]:
    k = st.columns(6)
    k[0].metric("Visites reçues", f"{len(visits):,}".replace(",", " "))
    k[1].metric("Visites abouties", int(visits["aboutie"].sum()), f"{visits['aboutie'].mean() * 100:.0f} %", delta_color="off", delta_arrow="off")
    k[2].metric("Prix relevés", f"{len(releves):,}".replace(",", " "), f"{aujourdhui} aujourd'hui", delta_color="off", delta_arrow="off")
    k[3].metric("Articles distincts", n_dist, f"sur {nomenc_total} à couvrir" if nomenc_total else None, delta_color="off", delta_arrow="off")
    k[4].metric("Produits différents", n_diff, f"{n_diff / len(releves) * 100:.1f} % des prix relevés" if len(releves) else None, delta_color="off", delta_arrow="off")
    k[5].metric("Dernière réception", visits["_submission_time"].max().strftime("%d/%m %H:%M"))
    c1, c2 = st.columns(2)
    par_enq = summarize(visits, items, "enqueteur")
    if not par_enq.empty and "Articles saisis" in par_enq:
        c1.plotly_chart(composition_chart(par_enq, "Enqueteur", "Articles saisis par enquêteur"), width="stretch")
    par_jour = summarize(visits, items, "jour")
    if not par_jour.empty:
        par_jour["Jour"] = pd.to_datetime(par_jour["Jour"])
        fig = px.bar(par_jour, x="Jour", y=["Visites", "Prix relevés"], barmode="group", title="Visites et prix relevés par jour")
        fig.update_layout(legend_title_text="", margin=dict(t=50, b=10))
        c2.plotly_chart(fig, width="stretch")
    st.subheader("Tableau de bord par enquêteur")
    st.dataframe(par_enq, hide_index=True, width="stretch")
    with st.expander("Définitions des indicateurs"):
        st.markdown(
            "- **Visites abouties** : fiches dont le résultat est « Visite aboutie (prix relevés) ».\n"
            "- **Articles saisis** : ensemble des lignes du bloc « Articles observés », quelle que soit la disponibilité.\n"
            "- **Prix relevés** : articles disponibles en stock ou sur commande, donc avec un prix.\n"
            "- **Produits différents** : prix relevés dont la conformité est « Différent ». Ils sont conservés mais exclus du référentiel.\n"
            "- **Observations retenues** : prix relevés dont le produit est identique ou équivalent à la nomenclature.\n"
            "- **Articles distincts** : nombre d'articles de la nomenclature différents parmi les prix relevés.\n"
            "- **Couverture** : articles distincts rapportés aux articles sélectionnables (statut « prêt »)."
        )

# ---- Enquêteurs (suite)
with T["Enquêteurs"]:
    if not par_enq.empty and "Articles saisis" in par_enq:
        c1, c2 = st.columns(2)
        fig = px.bar(par_enq, y="Enqueteur", x="Articles distincts", orientation="h", title="Articles distincts par enquêteur")
        fig.update_yaxes(categoryorder="total ascending")
        c1.plotly_chart(fig, width="stretch")
        if "Taux de produits différents (%)" in par_enq:
            fig = px.bar(par_enq, y="Enqueteur", x="Taux de produits différents (%)", orientation="h", title="Part de produits différents (%)")
            fig.update_yaxes(categoryorder="total ascending")
            c2.plotly_chart(fig, width="stretch")
    st.subheader("Prix relevés par enquêteur et par jour")
    if len(releves):
        heat = releves.assign(Jour=releves["jour"].dt.strftime("%d/%m")).pivot_table(
            index="enqueteur", columns="Jour", values="_id", aggfunc="count", fill_value=0)
        heat = heat[sorted(heat.columns, key=lambda s: (s[3:], s[:2]))]
        st.plotly_chart(px.imshow(heat, text_auto=True, aspect="auto", color_continuous_scale="Blues",
                                  labels=dict(x="Jour", y="Enquêteur", color="Prix relevés")), width="stretch")

# ---- Vagues et localités
with T["Vagues et localités"]:
    par_vague = summarize(visits, items, "vague")
    if not par_vague.empty and vague_order:
        par_vague["_o"] = par_vague["Vague"].map({v: n for n, v in enumerate(vague_order)})
        par_vague = par_vague.sort_values("_o").drop(columns="_o")
    st.subheader("Par vague")
    st.dataframe(par_vague, hide_index=True, width="stretch")
    if not par_vague.empty and "Articles saisis" in par_vague:
        st.plotly_chart(composition_chart(par_vague, "Vague", "Articles saisis par vague", horizontal=False), width="stretch")

    st.subheader("Par ville")
    par_ville = summarize(visits, items, "ville")
    st.dataframe(par_ville, hide_index=True, width="stretch")
    if not par_ville.empty and "Articles saisis" in par_ville:
        st.plotly_chart(composition_chart(par_ville, "Ville", "Articles saisis par ville"), width="stretch")

    st.subheader("Par quartier ou secteur")
    par_q = summarize(visits, items, "quartier")
    st.dataframe(par_q.sort_values("Visites", ascending=False) if "Visites" in par_q else par_q, hide_index=True, width="stretch", height=300)

    st.subheader("Localisation des visites")
    geo = visits.dropna(subset=["latitude", "longitude"])
    if len(geo):
        st.map(geo, latitude="latitude", longitude="longitude")
    else:
        st.info("Aucune coordonnée GPS disponible.")

# ---- Familles et articles
with T["Familles et articles"]:
    par_fam = summarize(visits, items, "famille", nomenclature=nomenc_fam)
    st.subheader("Par famille d'articles")
    st.dataframe(par_fam, hide_index=True, width="stretch")
    if not par_fam.empty and "Articles saisis" in par_fam:
        c1, c2 = st.columns(2)
        c1.plotly_chart(composition_chart(par_fam, "Famille", "Articles saisis par famille"), width="stretch")
        if "Couverture (%)" in par_fam:
            fig = px.bar(par_fam, y="Famille", x="Couverture (%)", orientation="h", title="Couverture de la nomenclature (%)",
                         range_x=[0, 100])
            fig.update_yaxes(categoryorder="total ascending")
            c2.plotly_chart(fig, width="stretch")

    st.subheader("Détail par article")
    fam_choices = sorted(items["famille"].dropna().unique())
    fam_sel = st.multiselect("Famille", fam_choices)
    base = items if not fam_sel else items[items["famille"].isin(fam_sel)]
    if len(base):
        det = base.groupby(["famille", "article"], dropna=False).agg(
            **{"Articles saisis": ("_id", "size"), "Prix relevés": ("releve", "sum"),
               "Produits différents": ("different", "sum"), "Fournisseurs": ("nom_fournisseur", "nunique"),
               "Prix min": ("prix_comparable", "min"), "Prix médian": ("prix_comparable", "median"),
               "Prix max": ("prix_comparable", "max")}
        ).reset_index().rename(columns={"famille": "Famille", "article": "Article"})
        st.caption("Les prix sont ramenés à l'unité de base, dans la base de prix saisie (HT ou TTC) et dans la devise de chaque fiche.")
        st.dataframe(det, hide_index=True, width="stretch", height=380)

        st.subheader("Tous les prix relevés pour un article")
        art_list = sorted(base.loc[base["releve"], "article"].dropna().unique())
        if art_list:
            art_sel = st.selectbox("Article", art_list)
            px_art = base[(base["article"] == art_sel) & base["releve"]].copy()
            px_art["jour"] = px_art["jour"].dt.strftime("%d/%m/%Y")
            cols = {"jour": "Jour", "enqueteur": "Enquêteur", "vague": "Vague", "ville": "Ville",
                    "nom_fournisseur": "Fournisseur", "conformite": "Conformité", "unite_vente": "Unité de vente",
                    "qte_prix": "Quantité du prix", "nb_unites": "Unités de base par unité de vente",
                    "prix_ht": "Prix HT", "prix_ttc": "Prix TTC", "prix_comparable": "Prix par unité de base",
                    "base_retenue": "Base", "devise": "Devise", "nature_source": "Source"}
            st.dataframe(px_art[list(cols)].rename(columns=cols).sort_values("Jour"), hide_index=True, width="stretch")
        else:
            st.info("Aucun prix relevé pour la sélection.")

    st.subheader("Articles de la nomenclature sans prix relevé")
    if nomenc_total:
        vus = set(releves["article_code"].dropna())
        manque = pret[~pret["name"].isin(vus)].copy()
        manque["Famille"] = manque["famille"].map(labels.get("familles", {}))
        manque = manque.rename(columns={"label": "Article"})[["Famille", "Article"]]
        if fam_sel:
            manque = manque[manque["Famille"].isin(fam_sel)]
        st.write(f"{len(manque)} article(s) sans prix relevé.")
        st.dataframe(manque, hide_index=True, width="stretch", height=320)

# ---- Suivi journalier
with T["Suivi journalier"]:
    if len(items):
        par_jour2 = summarize(visits, items, "jour")
        par_jour2["Jour"] = pd.to_datetime(par_jour2["Jour"])
        par_jour2 = par_jour2.sort_values("Jour")
        par_jour2["Prix relevés cumulés"] = par_jour2["Prix relevés"].cumsum()
        premier = releves.groupby("article_code")["jour"].min().value_counts().rename("Nouveaux articles")
        nouveaux = premier.rename_axis("Jour").reset_index().sort_values("Jour")
        nouveaux["Articles distincts cumulés"] = nouveaux["Nouveaux articles"].cumsum()

        c1, c2 = st.columns(2)
        c1.plotly_chart(px.line(par_jour2, x="Jour", y="Prix relevés cumulés", markers=True, title="Prix relevés cumulés"), width="stretch")
        if len(nouveaux):
            fig = px.line(nouveaux, x="Jour", y="Articles distincts cumulés", markers=True, title="Articles distincts cumulés")
            c2.plotly_chart(fig, width="stretch")
        aff = par_jour2.copy()
        aff["Jour"] = aff["Jour"].dt.strftime("%d/%m/%Y")
        st.dataframe(aff, hide_index=True, width="stretch")

        st.subheader("Détail d'une journée")
        jours = sorted(items["jour"].dropna().unique(), reverse=True)
        j = st.selectbox("Jour", jours, format_func=lambda d: pd.Timestamp(d).strftime("%d/%m/%Y"))
        jj = items[items["jour"] == j]
        st.dataframe(summarize(visits[visits["jour"] == j], jj, "enqueteur"), hide_index=True, width="stretch")
    else:
        st.info("Aucun article saisi pour le moment.")

# ---- Tableau croisé
with T["Tableau croisé"]:
    dims = {"Enquêteur": "enqueteur", "Vague": "vague", "Ville": "ville", "Famille": "famille", "Jour": "jour"}
    mesures = ["Visites", "Prix relevés", "Articles distincts", "Produits différents", "Observations retenues", "Articles saisis"]
    c1, c2, c3 = st.columns(3)
    ligne = c1.selectbox("Lignes", list(dims), index=0)
    colonne = c2.selectbox("Colonnes", list(dims), index=1)
    mesure = c3.selectbox("Mesure", mesures, index=1)
    if ligne == colonne:
        st.info("Choisir deux dimensions différentes.")
    elif mesure == "Visites" and "famille" in (dims[ligne], dims[colonne]):
        st.info("Le nombre de visites ne peut pas être ventilé par famille.")
    else:
        r, cc = dims[ligne], dims[colonne]
        if mesure == "Visites":
            src = visits.assign(_v=1)
            tab = src.pivot_table(index=r, columns=cc, values="_v", aggfunc="sum", fill_value=0, margins=True, margins_name="Total")
        else:
            src = items.copy()
            if mesure == "Prix relevés":
                src = src[src["releve"]]
            elif mesure == "Produits différents":
                src = src[src["different"]]
            elif mesure == "Observations retenues":
                src = src[src["retenue"]]
            elif mesure == "Articles distincts":
                src = src[src["releve"]]
            for d_ in (r, cc):
                src[d_] = src[d_].fillna(NR)
            if mesure == "Articles distincts":
                tab = src.pivot_table(index=r, columns=cc, values="article_code", aggfunc="nunique", fill_value=0)
                tab["Total"] = src.groupby(r)["article_code"].nunique()
            else:
                tab = src.pivot_table(index=r, columns=cc, values="_id", aggfunc="count", fill_value=0, margins=True, margins_name="Total")
        if isinstance(tab.columns[0], pd.Timestamp):
            tab.columns = [c.strftime("%d/%m") if isinstance(c, pd.Timestamp) else c for c in tab.columns]
        if isinstance(tab.index[0], pd.Timestamp):
            tab.index = [i.strftime("%d/%m/%Y") if isinstance(i, pd.Timestamp) else i for i in tab.index]
        st.dataframe(tab, width="stretch")
        if mesure == "Articles distincts":
            st.caption("Le total d'une ligne compte chaque article une seule fois, il n'est donc pas la somme des colonnes.")

# ---- Contrôle qualité
with T["Contrôle qualité"]:
    q = st.columns(4)

    vides = visits[visits["aboutie"] & (visits["nb_articles"].fillna(0) == 0)]
    q[0].metric("Visites abouties sans article", len(vides))

    phys_sans_gps = visits[visits["mode_collecte_code"] == "physique"]
    phys_sans_gps = phys_sans_gps[phys_sans_gps["latitude"].isna()]
    q[1].metric("Visites physiques sans GPS", len(phys_sans_gps))

    visits = visits.assign(_f=visits["nom_fournisseur"].fillna("").str.lower().str.strip())
    dup_v = visits[(visits["_f"] != "") & visits.duplicated(["enqueteur_code", "_f", "jour"], keep=False)]
    q[2].metric("Visites possiblement en double", len(dup_v))

    dup_a = items[items["article_code"].notna() & items.duplicated(["_id", "article_code"], keep=False)]
    q[3].metric("Article saisi deux fois dans une visite", len(dup_a))

    st.subheader("Produits différents à suivre avec le superviseur")
    diff = items[items["different"]][["id_visite", "enqueteur", "nom_fournisseur", "article", "conformite_prec", "jour"]]
    diff.columns = ["Visite", "Enquêteur", "Fournisseur", "Article", "Écart signalé", "Jour"]
    st.write(f"{len(diff)} produit(s) différent(s).")
    if len(diff):
        st.dataframe(diff, hide_index=True, width="stretch")

    if len(dup_v):
        st.subheader("Visites possiblement en double")
        st.dataframe(dup_v[["id_visite", "enqueteur", "nom_fournisseur", "jour", "resultat_visite", "nb_articles"]].sort_values(["enqueteur", "nom_fournisseur"]),
                     hide_index=True, width="stretch")
    if len(dup_a):
        st.subheader("Articles saisis deux fois dans une même visite")
        st.dataframe(dup_a[["id_visite", "enqueteur", "article", "prix_retenu", "disponibilite"]], hide_index=True, width="stretch")

# ---- Données
with T["Données"]:
    st.write("Export de la base : une ligne par article observé (feuille Articles) et une ligne par visite (feuille Visites), avec libellés et codes.")
    complet = st.checkbox("Exporter la base complète, sans tenir compte des filtres", value=True)
    exp_v = visits_all if complet else visits
    exp_i = items_all if complet else items
    drop = [c for c in ("jour_reception", "_f", "aboutie", "releve", "different", "retenue") if c in exp_v.columns or c in exp_i.columns]
    exp_v = exp_v.drop(columns=[c for c in drop if c in exp_v.columns]).rename(columns={"jour_visite": "jour_de_visite"})
    exp_i = exp_i.drop(columns=[c for c in ("jour_reception", "releve", "different", "retenue") if c in exp_i.columns]).rename(columns={"jour_visite": "jour_de_visite", "valide": "prix_valide"})
    exp_v = exp_v.drop(columns=[c for c in ("jour",) if c in exp_v.columns])
    exp_i = exp_i.drop(columns=[c for c in ("jour",) if c in exp_i.columns])

    choix = st.radio("Aperçu", ["Articles", "Visites"], horizontal=True)
    apercu = exp_i if choix == "Articles" else exp_v
    st.write(f"{len(apercu)} ligne(s), {apercu.shape[1]} colonne(s).")
    st.dataframe(apercu, hide_index=True, width="stretch", height=420)

    sheets = {
        "Articles": exp_i,
        "Visites": exp_v,
        "Par enquêteur": summarize(visits_all if complet else visits, items_all if complet else items, "enqueteur"),
        "Par vague": summarize(visits_all if complet else visits, items_all if complet else items, "vague"),
        "Par ville": summarize(visits_all if complet else visits, items_all if complet else items, "ville"),
        "Par famille": summarize(visits_all if complet else visits, items_all if complet else items, "famille", nomenclature=nomenc_fam),
    }
    src_i = items_all if complet else items
    art_exp = coverage_table(src_i[src_i["valide"]], pret, labels, seuil)
    sheets["Couverture articles"] = art_exp.drop(columns="article_code")
    sheets["Couverture familles"] = coverage_by_famille(art_exp, seuil)
    stamp = pd.Timestamp.now(tz=TZ).strftime("%Y%m%d_%H%M")
    d1, d2, d3 = st.columns(3)
    d1.download_button("Télécharger le classeur Excel", to_excel(sheets), file_name=f"collecte_prix_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d2.download_button("Articles en CSV", exp_i.to_csv(index=False).encode("utf-8-sig"), file_name=f"articles_{stamp}.csv", mime="text/csv")
    d3.download_button("Visites en CSV", exp_v.to_csv(index=False).encode("utf-8-sig"), file_name=f"visites_{stamp}.csv", mime="text/csv")
