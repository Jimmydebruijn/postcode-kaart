import re
import streamlit as st
import requests
import pandas as pd
import folium
from folium.features import GeoJsonTooltip
from streamlit_folium import st_folium
import plotly.express as px

st.set_page_config(page_title="Nederland Demografische Kaart", page_icon="🗺️", layout="wide")
st.title("🗺️ Nederland Demografische Kaart")
st.caption("Klik op een gemeente voor details • Bron: CBS StatLine (CC BY 4.0)")

BASE    = "https://opendata.cbs.nl/ODataApi/OData/83502NED"
HH_BASE = "https://opendata.cbs.nl/ODataApi/OData/83505NED"
HK_BASE = "https://opendata.cbs.nl/ODataApi/OData/85640NED"

GEOJSON_GEMEENTE = "https://cartomap.github.io/nl/wgs84/gemeente_2024.geojson"

LABEL_MAP = {
    "0 tot 5 jaar":"0-5","5 tot 10 jaar":"5-10","10 tot 15 jaar":"10-15",
    "15 tot 20 jaar":"15-20","20 tot 25 jaar":"20-25","25 tot 30 jaar":"25-30",
    "30 tot 35 jaar":"30-35","35 tot 40 jaar":"35-40","40 tot 45 jaar":"40-45",
    "45 tot 50 jaar":"45-50","50 tot 55 jaar":"50-55","55 tot 60 jaar":"55-60",
    "60 tot 65 jaar":"60-65","65 tot 70 jaar":"65-70","70 tot 75 jaar":"70-75",
    "75 tot 80 jaar":"75-80","80 tot 85 jaar":"80-85","85 tot 90 jaar":"85-90",
    "90 jaar of ouder":"90+",
}
GEWENSTE        = list(LABEL_MAP.keys())
LABELS_VOLGORDE = list(LABEL_MAP.values())
GEWICHTEN = {
    "0-5":2.5,"5-10":7.5,"10-15":12.5,"15-20":17.5,"20-25":22.5,
    "25-30":27.5,"30-35":32.5,"35-40":37.5,"40-45":42.5,"45-50":47.5,
    "50-55":52.5,"55-60":57.5,"60-65":62.5,"65-70":67.5,"70-75":72.5,
    "75-80":77.5,"80-85":82.5,"85-90":87.5,"90+":92.5,
}
HK_GEWENST = {
    "Totaal":"Totaal","Nederland":"Nederland",
    "Europa (exclusief Nederland)":"Europa (excl. NL)",
    "Afrika":"Afrika","Amerika":"Amerika","Azië":"Azië",
    "Turkije":"Turkije","Marokko":"Marokko","Suriname":"Suriname",
}
HK_CAT  = ["Nederland","Europa (excl. NL)","Turkije","Marokko","Suriname","Afrika","Amerika","Azië"]
HH_TYPEN = {
    "Eenpersoonshuishouden":"Alleenstaand",
    "Meerpersoonshuishouden met kinderen":"Gezin met kinderen",
    "Meerpersoonshuishouden zonder kinderen":"Stel/meerp. zonder kinderen",
}

# ── Session state ──────────────────────────────────────────────────────────────
if "geselecteerde_gemeente" not in st.session_state:
    st.session_state.geselecteerde_gemeente = None

# ── Fetch helpers ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def fetch(url):
    rows, nxt = [], url
    while nxt:
        r = requests.get(nxt, timeout=30)
        r.raise_for_status()
        d = r.json()
        rows.extend(d.get("value", []))
        nxt = d.get("odata.nextLink")
    return rows

@st.cache_data(ttl=7200, show_spinner="GeoJSON laden...")
def get_geojson(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

# ── CBS leeftijd per postcode ──────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_leeftijd_meta():
    perioden      = fetch(f"{BASE}/Perioden?$format=json")
    periode_key   = perioden[-1]["Key"]
    periode_title = perioden[-1]["Title"].strip()
    leeftijden    = fetch(f"{BASE}/Leeftijd?$format=json")
    leeftijd_map  = {l["Key"].strip(): l["Title"].strip() for l in leeftijden}
    leeftijd_keys = [k for k,v in leeftijd_map.items() if v in GEWENSTE]
    geslachten    = fetch(f"{BASE}/Geslacht?$format=json")
    geslacht_key  = next(g["Key"] for g in geslachten if "Totaal" in g["Title"])
    alle_pc       = fetch(f"{BASE}/Postcode?$format=json")
    pc_key_map    = {item["Title"].strip(): item["Key"] for item in alle_pc}
    return periode_key, periode_title, leeftijd_map, leeftijd_keys, geslacht_key, pc_key_map

@st.cache_data(ttl=3600)
def get_leeftijd_verd(pc_key, periode_key, geslacht_key, leeftijd_keys, leeftijd_map):
    out = {}
    for lkey in leeftijd_keys:
        obs = fetch(
            f"{BASE}/TypedDataSet?$format=json"
            f"&$filter=Perioden eq '{periode_key}' and Geslacht eq '{geslacht_key}'"
            f" and Postcode eq '{pc_key}' and Leeftijd eq '{lkey}'"
            f"&$select=Leeftijd,Bevolking_1"
        )
        for row in obs:
            label = LABEL_MAP.get(leeftijd_map.get(row.get("Leeftijd","").strip(),""))
            if label:
                out[label] = out.get(label,0) + (row.get("Bevolking_1") or 0)
    return out

# ── CBS kerncijfers per gemeente (85984NED) voor choropleth ───────────────────
@st.cache_data(ttl=7200, show_spinner="Gemeentedata laden (eenmalig ~30s)...")
def get_gemeente_kerncijfers():
    """
    Haal gemiddelde leeftijd per gemeente via CBS 85984NED.
    Codering = 'GM' + gemeentecode (4-cijferig, bijv. GM0363).
    """
    try:
        # Haal DataProperties om juiste kolomnaam te vinden
        props = fetch("https://opendata.cbs.nl/ODataApi/OData/85984NED/DataProperties?$format=json")
        leeftijd_col = next(
            (p["Key"] for p in props if "Leeftijd" in p.get("Title","") and "Gemiddeld" in p.get("Title","")),
            None
        )
        if not leeftijd_col:
            # Fallback: zoek op sleutelnaam
            leeftijd_col = next(
                (p["Key"] for p in props if "GemiddeldeLeeftijd" in p.get("Key","")),
                None
            )

        if not leeftijd_col:
            return {}, None

        # Haal alle gemeenten op (SoortRegio = 'Gemeente')
        obs = fetch(
            f"https://opendata.cbs.nl/ODataApi/OData/85984NED/TypedDataSet?$format=json"
            f"&$filter=SoortRegio eq 'Gemeente'"
            f"&$select=WijkenEnBuurten,{leeftijd_col}"
        )
        result = {}
        for row in obs:
            code = row.get("WijkenEnBuurten","").strip()  # bijv. "GM0363"
            val  = row.get(leeftijd_col)
            if code and val:
                result[code] = round(float(val), 1)
        return result, leeftijd_col
    except Exception as e:
        return {}, None

# ── CBS huishoudens per postcode ───────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_hh_meta():
    perioden = fetch(f"{HH_BASE}/Perioden?$format=json")
    per_key  = perioden[-1]["Key"]
    hh_typen = fetch(f"{HH_BASE}/Huishoudenssamenstelling?$format=json")
    hh_map   = {h["Key"].strip(): h["Title"].strip() for h in hh_typen}
    alle_pc  = fetch(f"{HH_BASE}/Postcode?$format=json")
    pc_map   = {item["Title"].strip(): item["Key"] for item in alle_pc}
    return per_key, hh_map, pc_map

@st.cache_data(ttl=3600)
def get_hh_data(pc_key, periode_key, hh_map):
    obs = fetch(
        f"{HH_BASE}/TypedDataSet?$format=json"
        f"&$filter=Perioden eq '{periode_key}' and Postcode eq '{pc_key}'"
        f"&$select=Huishoudenssamenstelling,ParticuliereHuishoudens_1,GemiddeldeHuishoudensgrootte_2"
    )
    d = {}
    for row in obs:
        titel = hh_map.get(row.get("Huishoudenssamenstelling","").strip(),"")
        if titel in HH_TYPEN:
            d[HH_TYPEN[titel]] = row.get("ParticuliereHuishoudens_1") or 0
        if titel == "Totaal particuliere huishoudens":
            d["__totaal"]  = row.get("ParticuliereHuishoudens_1") or 0
            d["__grootte"] = row.get("GemiddeldeHuishoudensgrootte_2") or 0
    return d

# ── CBS herkomst per postcode ──────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_hk_meta():
    perioden   = fetch(f"{HK_BASE}/Perioden?$format=json")
    per_key    = perioden[-1]["Key"]
    hk_landen  = fetch(f"{HK_BASE}/Herkomstland?$format=json")
    hk_map     = {h["Key"].strip(): h["Title"].strip() for h in hk_landen}
    gb_landen  = fetch(f"{HK_BASE}/Geboorteland?$format=json")
    gb_totaal  = next((g["Key"] for g in gb_landen if "Totaal" in g["Title"]), None)
    geslachten = fetch(f"{HK_BASE}/Geslacht?$format=json")
    gsl_key    = next(g["Key"] for g in geslachten if "Totaal" in g["Title"])
    alle_pc    = fetch(f"{HK_BASE}/Postcode?$format=json")
    pc_map     = {item["Title"].strip(): item["Key"] for item in alle_pc}
    return per_key, hk_map, gb_totaal, gsl_key, pc_map

@st.cache_data(ttl=3600)
def get_hk_data(pc_key, periode_key, gb_totaal, gsl_key, hk_map):
    obs = fetch(
        f"{HK_BASE}/TypedDataSet?$format=json"
        f"&$filter=Perioden eq '{periode_key}' and Geboorteland eq '{gb_totaal}'"
        f" and Geslacht eq '{gsl_key}' and Postcode eq '{pc_key}'"
        f"&$select=Herkomstland,Bevolking_1"
    )
    result = {}
    for row in obs:
        titel = hk_map.get(row.get("Herkomstland","").strip(),"")
        if titel in HK_GEWENST:
            result[HK_GEWENST[titel]] = row.get("Bevolking_1") or 0
    return result

# ── PDOK helpers ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def gemeente_van_coordinaten(lat, lon):
    """Reverse geocode: coördinaten → gemeentenaam via PDOK."""
    try:
        r = requests.get(
            "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse",
            params={"lat": lat, "lon": lon, "type": "gemeente", "rows": 1},
            timeout=6
        )
        if r.status_code == 200:
            docs = r.json().get("response",{}).get("docs",[])
            if docs:
                return docs[0].get("weergavenaam","").replace("Gemeente ","")
    except Exception:
        pass
    return None

@st.cache_data(ttl=3600)
def postcodes_van_gemeente(naam):
    alle, start = [], 0
    while True:
        try:
            r = requests.get(
                "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
                params={"q": naam, "fq": "type:postcode",
                        "fl": "weergavenaam,gemeentenaam", "rows": 100, "start": start},
                timeout=10
            )
            if r.status_code != 200: break
            data = r.json().get("response",{})
            docs = data.get("docs",[])
            if not docs: break
            for d in docs:
                if d.get("gemeentenaam","").lower() == naam.lower():
                    m = re.search(r'\b(\d{4})[A-Z]{2}\b', d.get("weergavenaam",""))
                    if m: alle.append(m.group(1))
            if start + 100 >= data.get("numFound",0): break
            start += 100
        except Exception:
            break
    return sorted(set(alle))

# ── Rekenhulpen ────────────────────────────────────────────────────────────────
def pct(verd):
    tot = sum(verd.values())
    return {k: v/tot*100 for k,v in verd.items()} if tot else {}

def gem_leeftijd_fn(verd):
    tot = sum(verd.values())
    return sum(GEWICHTEN[k]*v for k,v in verd.items())/tot if tot else None

def combineer(verds):
    out = {}
    for v in verds:
        for k,a in v.items(): out[k] = out.get(k,0)+a
    return out

# ── Data laden ─────────────────────────────────────────────────────────────────
with st.spinner("Metadata laden..."):
    periode_key, periode_title, leeftijd_map, leeftijd_keys, geslacht_key, pc_key_map = get_leeftijd_meta()
    hh_per_key, hh_map_meta, hh_pc_map = get_hh_meta()
    hk_per_key, hk_map_meta, gb_totaal, gsl_key, hk_pc_map = get_hk_meta()

gemeente_geojson = get_geojson(GEOJSON_GEMEENTE)
gemeente_kerncijfers, leeftijd_col = get_gemeente_kerncijfers()

# Voeg statcode en gem. leeftijd toe aan GeoJSON
for feat in gemeente_geojson["features"]:
    props = feat["properties"]
    # statcode in GeoJSON is bijv. "GM0363"
    statcode = props.get("statcode","")
    naam     = props.get("statnaam") or props.get("name","")
    props["display_naam"]  = naam
    props["gem_leeftijd"]  = gemeente_kerncijfers.get(statcode)
    props["leeftijd_label"]= f"{gemeente_kerncijfers[statcode]:.1f} jr" if statcode in gemeente_kerncijfers else "—"

# ── Zijbalk ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔍 Zoeken")
    zoek = st.text_input("Gemeente of postcode", placeholder="bijv. Amsterdam of 1013")

    if zoek:
        zoek = zoek.strip()
        if zoek.isdigit() and len(zoek) == 4:
            # Postcode → zoek gemeente
            try:
                r = requests.get(
                    "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
                    params={"q": zoek, "fq": "type:postcode", "rows": 1, "fl": "gemeentenaam,centroide_ll"},
                    timeout=5
                )
                if r.status_code == 200:
                    docs = r.json().get("response",{}).get("docs",[])
                    if docs:
                        gem_naam = docs[0].get("gemeentenaam","")
                        if gem_naam:
                            st.session_state.geselecteerde_gemeente = gem_naam
                            st.success(f"Gemeente: {gem_naam}")
            except Exception:
                pass
        else:
            st.session_state.geselecteerde_gemeente = zoek

    st.divider()
    if st.session_state.geselecteerde_gemeente:
        st.success(f"📍 {st.session_state.geselecteerde_gemeente}")
        if st.button("✖ Selectie wissen"):
            st.session_state.geselecteerde_gemeente = None
            st.rerun()

    st.divider()
    st.caption(f"Peiljaar: {periode_title}")
    st.caption("Choropleth: gemiddelde leeftijd per gemeente")
    st.caption("Klik op een gemeente voor demografische details")

# ── Kaart bouwen ───────────────────────────────────────────────────────────────
m = folium.Map(location=[52.15, 5.3], zoom_start=7,
               tiles="CartoDB positron", prefer_canvas=True)

# Choropleth — gem. leeftijd per gemeente
if gemeente_kerncijfers:
    choropleth = folium.Choropleth(
        geo_data=gemeente_geojson,
        data=pd.Series(gemeente_kerncijfers),
        key_on="feature.properties.statcode",
        fill_color="RdYlGn_r",
        fill_opacity=0.75,
        line_opacity=0.2,
        line_color="white",
        legend_name="Gemiddelde leeftijd (jaar)",
        name="Gem. leeftijd",
        nan_fill_color="#dddddd",
        nan_fill_opacity=0.4,
    )
    choropleth.add_to(m)

# Klikbare transparante laag met tooltip
folium.GeoJson(
    gemeente_geojson,
    name="Gemeentegrenzen",
    style_function=lambda f: {
        "fillOpacity": 0,
        "weight": 0.8,
        "color": "#555",
    },
    highlight_function=lambda f: {
        "fillOpacity": 0.25,
        "fillColor": "#1D9E75",
        "weight": 2.5,
        "color": "#1D9E75",
    },
    tooltip=GeoJsonTooltip(
        fields=["display_naam", "leeftijd_label"],
        aliases=["Gemeente:", "Gem. leeftijd:"],
        style="font-size:13px; font-family:sans-serif; padding:6px;",
        sticky=True,
    ),
).add_to(m)

folium.LayerControl().add_to(m)

# ── Layout ─────────────────────────────────────────────────────────────────────
col_kaart, col_detail = st.columns([3, 2], gap="medium")

with col_kaart:
    kaart_data = st_folium(
        m,
        width="100%",
        height=620,
        returned_objects=["last_clicked"],
        key="kaart",
    )

    # Gemeente detecteren via klik-coördinaten → reverse geocode
    if kaart_data and kaart_data.get("last_clicked"):
        klik = kaart_data["last_clicked"]
        lat, lon = klik.get("lat"), klik.get("lng")
        if lat and lon:
            with st.spinner("Gemeente bepalen..."):
                gevonden = gemeente_van_coordinaten(lat, lon)
            if gevonden and gevonden != st.session_state.geselecteerde_gemeente:
                st.session_state.geselecteerde_gemeente = gevonden
                st.rerun()

# ── Detailpaneel ───────────────────────────────────────────────────────────────
with col_detail:
    gemeente = st.session_state.geselecteerde_gemeente

    if not gemeente:
        st.markdown("""
        ### 👈 Hoe te gebruiken
        1. **Klik op een gemeente** op de kaart
        2. Of **zoek** een gemeente of postcode links
        3. Bekijk leeftijd, huishoudens en herkomst in de tabs

        De kaart is ingekleurd op **gemiddelde leeftijd** — rood = ouder, groen = jonger.
        """)
    else:
        st.subheader(f"📍 {gemeente}")

        with st.spinner(f"Postcodes van {gemeente} ophalen..."):
            gem_pcs = postcodes_van_gemeente(gemeente)

        if not gem_pcs:
            st.warning(f"Geen postcodes gevonden voor {gemeente}.")
        else:
            # Postcode selector
            pc_keuze = st.selectbox(
                "Detailniveau",
                ["📊 Hele gemeente"] + gem_pcs,
                key="pc_keuze",
                help="Kies 'Hele gemeente' voor een totaalbeeld, of één postcode voor specifiek detail"
            )

            pcs_laden = gem_pcs if pc_keuze == "📊 Hele gemeente" else [pc_keuze]
            label_titel = gemeente if pc_keuze == "📊 Hele gemeente" else f"Postcode {pc_keuze}"

            st.caption(f"{len(gem_pcs)} postcodes in {gemeente} | Toon: {label_titel}")

            with st.spinner("CBS data laden..."):
                # Leeftijd
                verdelingen = {}
                for pc in pcs_laden:
                    key = pc_key_map.get(pc)
                    if key:
                        v = get_leeftijd_verd(key, periode_key, geslacht_key, leeftijd_keys, leeftijd_map)
                        if v: verdelingen[pc] = v

                # Huishoudens (max 30 voor snelheid)
                hh_results = {}
                for pc in pcs_laden[:30]:
                    key = hh_pc_map.get(pc)
                    if key:
                        d = get_hh_data(key, hh_per_key, hh_map_meta)
                        if d: hh_results[pc] = d

                # Herkomst (max 30)
                hk_results = {}
                for pc in pcs_laden[:30]:
                    key = hk_pc_map.get(pc)
                    if key:
                        d = get_hk_data(key, hk_per_key, gb_totaal, gsl_key, hk_map_meta)
                        if d: hk_results[pc] = d

            if not verdelingen:
                st.warning("Geen CBS data gevonden.")
            else:
                verd_agg = combineer(list(verdelingen.values()))
                hh_agg   = combineer([d for d in hh_results.values()])
                hk_agg   = combineer([d for d in hk_results.values()])

                tab1, tab2, tab3 = st.tabs(["👥 Leeftijd", "🏠 Huishoudens", "🌍 Herkomst"])

                with tab1:
                    tot = sum(verd_agg.values())
                    gem = gem_leeftijd_fn(verd_agg)
                    oud = sum(v for k,v in verd_agg.items() if k in ["65-70","70-75","75-80","80-85","85-90","90+"])
                    jong= sum(v for k,v in verd_agg.items() if k in ["0-5","5-10","10-15","15-20","20-25"])

                    c1,c2 = st.columns(2)
                    c1.metric("Inwoners",      f"{int(tot):,}".replace(",","."))
                    c2.metric("Gem. leeftijd", f"{gem:.1f} jaar" if gem else "—")
                    c3,c4 = st.columns(2)
                    c3.metric("Aandeel 65+",  f"{oud/tot*100:.1f}%")
                    c4.metric("Aandeel 0-25", f"{jong/tot*100:.1f}%")

                    df = pd.DataFrame([
                        {"Leeftijdsgroep": lbl, "%": round(pct(verd_agg).get(lbl,0),1)}
                        for lbl in LABELS_VOLGORDE
                    ])
                    fig = px.bar(df, x="Leeftijdsgroep", y="%",
                                 color_discrete_sequence=["#1D9E75"],
                                 height=240)
                    fig.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                                      xaxis=dict(tickangle=-45, tickfont=dict(size=9)),
                                      yaxis=dict(showgrid=True, gridcolor="#eee", title=""),
                                      margin=dict(t=8, b=50, l=30, r=8),
                                      showlegend=False)
                    st.plotly_chart(fig, use_container_width=True)

                with tab2:
                    if not hh_agg:
                        st.info("Geen huishoudensdata.")
                    else:
                        tot_hh = hh_agg.get("__totaal",1) or 1
                        c1,c2 = st.columns(2)
                        c1.metric("Huishoudens", f"{int(tot_hh):,}".replace(",","."))
                        c2.metric("Gem. grootte", f"{hh_agg.get('__grootte',0):.1f} pers.")
                        pie = {k:v for k,v in hh_agg.items() if not k.startswith("__") and v>0}
                        if pie:
                            fig_p = px.pie(names=list(pie.keys()), values=list(pie.values()),
                                           color_discrete_sequence=["#1D9E75","#185FA5","#BA7517"],
                                           hole=0.45, height=240)
                            fig_p.update_layout(margin=dict(t=8,b=8,l=8,r=8),
                                                legend=dict(font=dict(size=10)))
                            st.plotly_chart(fig_p, use_container_width=True)

                with tab3:
                    if not hk_agg:
                        st.info("Geen herkomstdata.")
                    else:
                        tot_hk = hk_agg.get("Totaal",1) or 1
                        pct_nl = hk_agg.get("Nederland",0)/tot_hk*100
                        c1,c2 = st.columns(2)
                        c1.metric("Herkomst NL",      f"{pct_nl:.1f}%")
                        c2.metric("Herkomst buiten NL",f"{100-pct_nl:.1f}%")
                        hk_df = pd.DataFrame([
                            {"Herkomst": cat, "%": round(hk_agg.get(cat,0)/tot_hk*100,1)}
                            for cat in HK_CAT if hk_agg.get(cat,0)>0
                        ])
                        if not hk_df.empty:
                            fig_h = px.bar(hk_df, x="%", y="Herkomst", orientation="h",
                                           color_discrete_sequence=["#534AB7"],
                                           height=260)
                            fig_h.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                                                xaxis=dict(showgrid=True, gridcolor="#eee"),
                                                yaxis=dict(autorange="reversed"),
                                                margin=dict(t=8, b=30, l=8, r=8),
                                                showlegend=False)
                            st.plotly_chart(fig_h, use_container_width=True)

st.divider()
st.caption("Data: CBS StatLine (CC BY 4.0) | Grenzen: cartomap.github.io | Geodata: PDOK Locatieserver")
