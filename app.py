"""Tableau de bord de suivi de la collecte des prix (UNICEF Burkina Faso).

Lecture en continu des soumissions KoboToolbox (API v2), suivi par enquêteur,
vague, localité, famille d'articles et jour, contrôles qualité et export final.
"""

import io
import os
from pathlib import Path

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
}

VISIT_COLS = [
    "_id", "_uuid", "_submission_time", "_submitted_by", "id_visite", "today", "start", "end",
    "enqueteur", "enqueteur_2", "vague", "ville", "ville_autre", "quartier", "quartier_autre",
    "adresse", "gps", "mode_collecte", "nom_fournisseur", "collecte_possible", "motif_impossible",
    "resultat_visite", "resultat_precision", "nb_articles", "commentaire_visite",
]
ITEM_COLS = [
    "_id", "famille", "article", "disponibilite", "quantite_stock", "conformite", "conformite_prec",
    "marque", "etat", "origine", "unite_vente", "unite_base", "nb_unites", "qte_prix", "devise",
    "base_prix", "prix_ht", "prix_ttc", "prix_retenu", "base_retenue", "prix_comparable",
    "livraison", "niveau_prix", "conditions_paiement", "nature_source", "date_obs", "commentaire",
]
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
    pwd = get_conf("APP_PASSWORD")
    if not pwd or st.session_state.get("auth_ok"):
        return True
    st.title("Accès restreint")
    saisie = st.text_input("Mot de passe", type="password")
    if saisie:
        if saisie == pwd:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    return False


if not check_password():
    st.stop()

st.sidebar.header("Connexion KoboToolbox")
server = st.sidebar.text_input("Serveur", get_conf("KOBO_SERVER", "https://kf.kobotoolbox.org"))
token = st.sidebar.text_input("Jeton API", get_conf("KOBO_TOKEN"), type="password")
asset_uid = st.sidebar.text_input("Identifiant du formulaire", get_conf("KOBO_ASSET_UID"))

st.sidebar.header("Actualisation")
interval = st.sidebar.selectbox("Actualisation automatique", ["Désactivée", "1 minute", "5 minutes", "15 minutes"], index=2)
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
    return rows


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
    di["article_code"] = di["article"]
    di["famille_code"] = di["famille"]

    keep = ["_id", "id_visite", "enqueteur", "vague", "ville", "quartier", "nom_fournisseur",
            "mode_collecte", "jour_visite", "jour_reception"]
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
st.caption("UNICEF Burkina Faso. Données lues en continu depuis KoboToolbox.")

if not (server and token and asset_uid):
    st.info("Renseigner le serveur, le jeton API et l'identifiant du formulaire dans la barre latérale.")
    st.stop()

try:
    records = fetch_data(server, token, asset_uid)
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
ref = st.sidebar.radio("Date de référence", ["Date de la visite", "Date de réception"], help="Date saisie sur le terminal ou date d'arrivée de la fiche sur le serveur.")
ref_col = "jour_visite" if ref == "Date de la visite" else "jour_reception"
visits_all["jour"] = visits_all[ref_col]
items_all["jour"] = items_all[ref_col]

dmin, dmax = visits_all["jour"].min().date(), visits_all["jour"].max().date()
periode = st.sidebar.date_input("Période", (dmin, dmax), min_value=dmin, max_value=dmax)

vague_order = [v for v in labels.get("vagues", {}).values() if v in set(visits_all["vague"].dropna())]
sel = {}
sel["vague"] = st.sidebar.multiselect("Vague", vague_order or sorted(visits_all["vague"].dropna().unique()))
sel["ville"] = st.sidebar.multiselect("Ville", sorted(visits_all["ville"].dropna().unique()))
sel["enqueteur"] = st.sidebar.multiselect("Enquêteur", sorted(visits_all["enqueteur"].dropna().unique()))

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

k = st.columns(6)
k[0].metric("Visites reçues", f"{len(visits):,}".replace(",", " "))
k[1].metric("Visites abouties", int(visits["aboutie"].sum()), f"{visits['aboutie'].mean() * 100:.0f} %", delta_color="off")
k[2].metric("Prix relevés", f"{len(releves):,}".replace(",", " "), f"{aujourdhui} aujourd'hui", delta_color="off")
k[3].metric("Articles distincts", n_dist, f"sur {nomenc_total} à couvrir" if nomenc_total else None, delta_color="off")
k[4].metric("Produits différents", n_diff, f"{n_diff / len(releves) * 100:.1f} % des prix relevés" if len(releves) else None, delta_color="off")
k[5].metric("Dernière réception", visits["_submission_time"].max().strftime("%d/%m %H:%M"))

tabs = st.tabs(["Synthèse", "Enquêteurs", "Vagues et localités", "Familles et articles",
                "Suivi journalier", "Tableau croisé", "Contrôle qualité", "Données"])

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


# ---- Synthèse
with tabs[0]:
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

# ---- Enquêteurs
with tabs[1]:
    st.dataframe(par_enq, hide_index=True, width="stretch")
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
with tabs[2]:
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
with tabs[3]:
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
with tabs[4]:
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
with tabs[5]:
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
with tabs[6]:
    q = st.columns(4)

    vides = visits[visits["aboutie"] & (visits["nb_articles"].fillna(0) == 0)]
    q[0].metric("Visites abouties sans article", len(vides))

    phys_sans_gps = visits[(visits["mode_collecte"] == labels.get("modes", {}).get("physique", "physique")) |
                           (visits["mode_collecte"] == "physique")]
    phys_sans_gps = phys_sans_gps[phys_sans_gps["latitude"].isna()]
    q[1].metric("Visites physiques sans GPS", len(phys_sans_gps))

    visits = visits.assign(_f=visits["nom_fournisseur"].fillna("").str.lower().str.strip())
    dup_v = visits[(visits["_f"] != "") & visits.duplicated(["enqueteur_code", "_f", "jour"], keep=False)]
    q[2].metric("Visites possiblement en double", len(dup_v))

    dup_a = items[items["article_code"].notna() & items.duplicated(["_id", "article_code"], keep=False)]
    q[3].metric("Article saisi deux fois dans une visite", len(dup_a))

    st.subheader("Durée de remplissage")
    dd = visits[visits["aboutie"]]["duree_min"].dropna()
    if len(dd):
        seuil = st.slider("Seuil de durée suspecte (minutes)", 0.0, float(max(dd.quantile(0.95), 5)), 3.0, step=0.5)
        courtes = visits[visits["aboutie"] & (visits["duree_min"] < seuil)][
            ["id_visite", "enqueteur", "nom_fournisseur", "jour", "duree_min", "nb_articles"]]
        dur = visits[visits["aboutie"]].groupby("enqueteur")["duree_min"].agg(["median", "min", "max"]).round(1).reset_index()
        dur.columns = ["Enquêteur", "Durée médiane (min)", "Durée minimale (min)", "Durée maximale (min)"]
        st.dataframe(dur, hide_index=True, width="stretch")
        st.write(f"{len(courtes)} visite(s) abouties plus courtes que {seuil:g} min.")
        if len(courtes):
            st.dataframe(courtes.sort_values("duree_min"), hide_index=True, width="stretch")

    st.subheader("Prix atypiques")
    st.caption("Prix par unité de base s'écartant de plus de 3 fois (ou moins du tiers) de la médiane de l'article, pour les articles ayant au moins 3 observations retenues.")
    ret = items[items["retenue"] & (items["prix_comparable"] > 0)].copy()
    if len(ret):
        stats = ret.groupby("article_code")["prix_comparable"].agg(["median", "count"])
        ret = ret.join(stats, on="article_code")
        atyp = ret[(ret["count"] >= 3) & ((ret["prix_comparable"] > 3 * ret["median"]) | (ret["prix_comparable"] < ret["median"] / 3))]
        atyp = atyp[["id_visite", "enqueteur", "nom_fournisseur", "article", "prix_comparable", "median", "base_retenue", "devise"]]
        atyp.columns = ["Visite", "Enquêteur", "Fournisseur", "Article", "Prix par unité de base", "Médiane de l'article", "Base", "Devise"]
        st.write(f"{len(atyp)} observation(s) atypiques.")
        if len(atyp):
            st.dataframe(atyp, hide_index=True, width="stretch")
    else:
        st.write("Pas encore assez de données.")

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
with tabs[7]:
    st.write("Export de la base : une ligne par article observé (feuille Articles) et une ligne par visite (feuille Visites), avec libellés et codes.")
    complet = st.checkbox("Exporter la base complète, sans tenir compte des filtres", value=True)
    exp_v = visits_all if complet else visits
    exp_i = items_all if complet else items
    drop = [c for c in ("jour_reception", "_f", "aboutie", "releve", "different", "retenue") if c in exp_v.columns or c in exp_i.columns]
    exp_v = exp_v.drop(columns=[c for c in drop if c in exp_v.columns]).rename(columns={"jour_visite": "jour_de_visite"})
    exp_i = exp_i.drop(columns=[c for c in ("jour_reception", "releve", "different", "retenue") if c in exp_i.columns]).rename(columns={"jour_visite": "jour_de_visite"})
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
    stamp = pd.Timestamp.now(tz=TZ).strftime("%Y%m%d_%H%M")
    d1, d2, d3 = st.columns(3)
    d1.download_button("Télécharger le classeur Excel", to_excel(sheets), file_name=f"collecte_prix_{stamp}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d2.download_button("Articles en CSV", exp_i.to_csv(index=False).encode("utf-8-sig"), file_name=f"articles_{stamp}.csv", mime="text/csv")
    d3.download_button("Visites en CSV", exp_v.to_csv(index=False).encode("utf-8-sig"), file_name=f"visites_{stamp}.csv", mime="text/csv")
