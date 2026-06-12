import streamlit as st
import requests
import pandas as pd
import folium
from folium.features import GeoJsonTooltip
from streamlit_folium import st_folium
import plotly.express as px
import json

st.set_page_config(
    page_title="Nederland Demografische Kaart",
    page_icon="🗺️",
    layout="wide"
)

st.title("🗺️ Nederland Demografische Kaart")
st.caption("Klik op een gemeente voor details • Zoom in voor postcodegebieden • Bron: CBS StatLine (CC BY 4.0)")

# ── Constanten ─────────────────────────────────────────────────────────────────
BASE     = "https://opendata.cbs.nl/ODataApi/OData/83502NED"
HH_BASE  = "https://opendata.cbs.nl/ODataApi/OData/83505NED"
HK_BASE  = "https://opendata.cbs.nl/ODataApi/OData/85640NED"

GEOJSON_GEMEENTE = "https://cartomap.github.io/nl/wgs84/gemeente_2024.geojson"
GEOJSON_PC4      = "https://cartomap.github.io/nl/wgs84/postcode4_2024.geojson"

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
HK_CAT_ORDER = ["Nederland","Europa (excl. NL)","Turkije","Marokko","Suriname","Afrika","Amerika","Azië"]
HH_TYPEN = {
    "Eenpersoonshuishouden":"Alleenstaand",
    "Meerpersoonshuishouden met kinderen":"Gezin met kinderen",
    "Meerpersoonshuishouden zonder kinderen":"Stel/meerp. zonder kinderen",
}

# ── CBS fetch ──────────────────────────────────────────────────────────────────
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

@st.cache_data(ttl=3600)
def get_geojson(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

# ── Leeftijd meta ──────────────────────────────────────────────────────────────
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

# ── Gemeente-niveau leeftijdsdata (CBS kerncijfers per gemeente) ───────────────
@st.cache_data(ttl=7200, show_spinner="Gemeentedata laden (eenmalig)...")
def get_gemeente_leeftijd():
    """
    Haal gemiddelde leeftijd per gemeente op via CBS tabel 85318NED
    (Kerncijfers wijken en buurten) — bevat GemiddeldeLeeftijd direct.
    """
    try:
        # Probeer CBS Wijk- en buurtkaart tabel voor gemiddelde leeftijd per gemeente
        r = requests.get(
            "https://opendata.cbs.nl/ODataApi/OData/85318NED/TypedDataSet"
            "?$format=json&$filter=SoortRegio eq 'Gemeente'&$select=Codering,GemiddeldeLeeftijd_P1&$top=500",
            timeout=30
        )
        if r.status_code == 200:
            data = r.json().get("value", [])
            return {row["Codering"].strip(): row.get("GemiddeldeLeeftijd_P1") for row in data if row.get("GemiddeldeLeeftijd_P1")}
    except Exception:
        pass
    return {}

# ── Huishoudens data per postcode ──────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_hh_meta():
    perioden  = fetch(f"{HH_BASE}/Perioden?$format=json")
    per_key   = perioden[-1]["Key"]
    hh_typen  = fetch(f"{HH_BASE}/Huishoudenssamenstelling?$format=json")
    hh_map    = {h["Key"].strip(): h["Title"].strip() for h in hh_typen}
    alle_pc   = fetch(f"{HH_BASE}/Postcode?$format=json")
    pc_map    = {item["Title"].strip(): item["Key"] for item in alle_pc}
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

# ── Herkomst data per postcode ─────────────────────────────────────────────────
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

# ── Hulpfuncties ───────────────────────────────────────────────────────────────
def pct(verd):
    tot = sum(verd.values())
    return {k: v/tot*100 for k,v in verd.items()} if tot else {}

def gem_leeftijd_verd(verd):
    tot = sum(verd.values())
    if not tot: return None
    return sum(GEWICHTEN[k]*v for k,v in verd.items()) / tot

def combineer(verds):
    out = {}
    for v in verds:
        for k,a in v.items(): out[k] = out.get(k,0)+a
    return out

# ── PDOK plaatsnaam → postcodelijst ────────────────────────────────────────────
@st.cache_data(ttl=3600)
def zoek_postcodes_van_gemeente(naam):
    try:
        r = requests.get(
            "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
            params={"q": naam, "fq": "type:postcode", "fl": "weergavenaam,gemeentenaam",
                    "rows": 100},
            timeout=8
        )
        if r.status_code == 200:
            docs = r.json().get("response",{}).get("docs",[])
            import re
            pcs = set()
            for d in docs:
                if d.get("gemeentenaam","").lower() == naam.lower():
                    m = re.search(r'\b(\d{4})[A-Z]{2}\b', d.get("weergavenaam",""))
                    if m: pcs.add(m.group(1))
            return sorted(pcs)
    except Exception:
        pass
    return []

# ── Laden ──────────────────────────────────────────────────────────────────────
with st.spinner("Metadata laden..."):
    periode_key, periode_title, leeftijd_map, leeftijd_keys, geslacht_key, pc_key_map = get_leeftijd_meta()
    hh_per_key, hh_map_meta, hh_pc_map = get_hh_meta()
    hk_per_key, hk_map_meta, gb_totaal, gsl_key, hk_pc_map = get_hk_meta()

with st.spinner("GeoJSON gemeentegrenzen laden..."):
    try:
        gemeente_geojson = get_geojson(GEOJSON_GEMEENTE)
        gemeente_namen = sorted([
            f["properties"].get("statnaam") or f["properties"].get("name","")
            for f in gemeente_geojson["features"]
        ])
    except Exception as e:
        st.error(f"Kon gemeentegrenzen niet laden: {e}")
        st.stop()

gemeente_leeftijd = get_gemeente_leeftijd()

# ── Zijbalk ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔍 Zoeken")
    zoek_naam = st.text_input("Gemeente of postcode", placeholder="bijv. Haarlem of 2011")

    st.divider()
    st.header("🎨 Kaart inkleuren op")
    kleur_keuze = st.radio("", ["Gem. leeftijd", "Aandeel 65+", "Aandeel 0-25"], label_visibility="collapsed")

    st.divider()
    st.caption(f"Peiljaar: {periode_title}")
    st.caption("Klik op een gemeente op de kaart voor demografische details")

# ── Kaart opbouwen ─────────────────────────────────────────────────────────────
# Voeg leeftijdsdata toe aan GeoJSON properties voor choropleth
for feature in gemeente_geojson["features"]:
    props = feature["properties"]
    stat_code = props.get("statcode","")
    gem_leeft = gemeente_leeftijd.get(stat_code)
    props["gem_leeftijd"] = round(gem_leeft, 1) if gem_leeft else None
    props["display_naam"] = props.get("statnaam") or props.get("name","")

# Startpositie bepalen
start_lat, start_lon, start_zoom = 52.15, 5.3, 7

# Als er gezocht wordt: zoom naar gemeente
if zoek_naam and zoek_naam.strip():
    zoek = zoek_naam.strip()
    if zoek.isdigit() and len(zoek) == 4:
        # Postcode → zoom in
        start_zoom = 12
        try:
            r = requests.get(
                "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free",
                params={"q": zoek, "fq": "type:postcode", "rows": 1, "fl": "centroide_ll,woonplaatsnaam"},
                timeout=5
            )
            if r.status_code == 200:
                docs = r.json().get("response",{}).get("docs",[])
                if docs:
                    centroide = docs[0].get("centroide_ll","")
                    import re
                    m = re.search(r'POINT\(([0-9.]+)\s+([0-9.]+)\)', centroide)
                    if m:
                        start_lon, start_lat = float(m.group(1)), float(m.group(2))
        except Exception:
            pass
    else:
        # Gemeente naam → zoom naar gemeente
        for feature in gemeente_geojson["features"]:
            naam = feature["properties"].get("statnaam","") or feature["properties"].get("name","")
            if naam.lower() == zoek.lower():
                import json as _json
                coords = feature["geometry"]["coordinates"]
                all_lats = []
                all_lons = []
                def extract_coords(c):
                    if isinstance(c[0], list):
                        for sub in c: extract_coords(sub)
                    else:
                        all_lons.append(c[0]); all_lats.append(c[1])
                extract_coords(coords)
                if all_lats:
                    start_lat = sum(all_lats)/len(all_lats)
                    start_lon = sum(all_lons)/len(all_lons)
                    start_zoom = 11
                break

# Bouw Folium kaart
m = folium.Map(location=[start_lat, start_lon], zoom_start=start_zoom,
               tiles="CartoDB positron", prefer_canvas=True)

# Choropleth laag op basis van gem. leeftijd
leeftijd_values = {
    f["properties"].get("statcode",""): f["properties"].get("gem_leeftijd")
    for f in gemeente_geojson["features"]
    if f["properties"].get("gem_leeftijd")
}

if leeftijd_values:
    folium.Choropleth(
        geo_data=gemeente_geojson,
        data=pd.Series(leeftijd_values),
        key_on="feature.properties.statcode",
        fill_color="RdYlGn_r",
        fill_opacity=0.7,
        line_opacity=0.3,
        line_color="white",
        legend_name="Gemiddelde leeftijd",
        name="Gem. leeftijd per gemeente",
        nan_fill_color="#cccccc",
        nan_fill_opacity=0.3,
    ).add_to(m)

# Interactieve laag met tooltip + klik
def style_fn(feature):
    return {"fillOpacity": 0, "weight": 0.8, "color": "#666"}

def highlight_fn(feature):
    return {"fillOpacity": 0.3, "fillColor": "#1D9E75", "weight": 2, "color": "#1D9E75"}

gemeente_layer = folium.GeoJson(
    gemeente_geojson,
    name="Gemeenten",
    style_function=style_fn,
    highlight_function=highlight_fn,
    tooltip=GeoJsonTooltip(
        fields=["display_naam", "gem_leeftijd"],
        aliases=["Gemeente:", "Gem. leeftijd:"],
        style="font-size:13px; font-family:sans-serif;",
        sticky=True,
    ),
).add_to(m)

folium.LayerControl().add_to(m)

# ── Layout: kaart links, detail rechts ────────────────────────────────────────
col_kaart, col_detail = st.columns([3, 2])

with col_kaart:
    kaart_output = st_folium(
        m,
        width="100%",
        height=600,
        returned_objects=["last_object_clicked_tooltip", "last_active_drawing"],
        key="hoofdkaart",
    )

# ── Detecteer aangeklikte gemeente ────────────────────────────────────────────
geselecteerd = None
if kaart_output and kaart_output.get("last_object_clicked_tooltip"):
    tooltip_data = kaart_output["last_object_clicked_tooltip"]
    if isinstance(tooltip_data, dict):
        geselecteerd = tooltip_data.get("display_naam") or tooltip_data.get("statnaam")

# Override vanuit zoekveld
if zoek_naam and not zoek_naam.strip().isdigit():
    geselecteerd = zoek_naam.strip()

# ── Detailpaneel ───────────────────────────────────────────────────────────────
with col_detail:
    if not geselecteerd:
        st.info("👈 Klik op een gemeente op de kaart, of zoek een gemeente / postcode links.")
    else:
        st.subheader(f"📍 {geselecteerd}")

        # Postcodes van gemeente ophalen
        with st.spinner(f"Postcodes van {geselecteerd} laden..."):
            gem_pcs = zoek_postcodes_van_gemeente(geselecteerd)

        if not gem_pcs:
            st.warning(f"Geen postcodes gevonden voor {geselecteerd}.")
        else:
            st.caption(f"{len(gem_pcs)} postcodes • {periode_title}")

            # Postcode selectie
            pc_keuze = st.selectbox(
                "Postcode selecteren (of bekijk heel de gemeente)",
                ["📊 Hele gemeente"] + gem_pcs,
                key="pc_select"
            )

            toon_gemeente = pc_keuze == "📊 Hele gemeente"
            pcs_te_laden = gem_pcs if toon_gemeente else [pc_keuze]

            # Data laden
            with st.spinner("Data laden..."):
                verdelingen = {}
                for pc in pcs_te_laden:
                    key = pc_key_map.get(pc)
                    if key:
                        v = get_leeftijd_verd(key, periode_key, geslacht_key, leeftijd_keys, leeftijd_map)
                        if v: verdelingen[pc] = v

                hh_results = {}
                for pc in pcs_te_laden[:20]:  # max 20 voor snelheid
                    key = hh_pc_map.get(pc)
                    if key:
                        d = get_hh_data(key, hh_per_key, hh_map_meta)
                        if d: hh_results[pc] = d

                hk_results = {}
                for pc in pcs_te_laden[:20]:
                    key = hk_pc_map.get(pc)
                    if key:
                        d = get_hk_data(key, hk_per_key, gb_totaal, gsl_key, hk_map_meta)
                        if d: hk_results[pc] = d

            if not verdelingen:
                st.warning("Geen CBS-data beschikbaar.")
            else:
                # Aggregeren
                verd_totaal = combineer(list(verdelingen.values()))
                hh_totaal   = {}
                for d in hh_results.values():
                    for k,v in d.items():
                        hh_totaal[k] = hh_totaal.get(k,0) + v

                hk_totaal = {}
                for d in hk_results.values():
                    for k,v in d.items():
                        hk_totaal[k] = hk_totaal.get(k,0) + v

                # Tabs
                dt1, dt2, dt3 = st.tabs(["👥 Leeftijd", "🏠 Huishoudens", "🌍 Herkomst"])

                with dt1:
                    tot = sum(verd_totaal.values())
                    gem = gem_leeftijd_verd(verd_totaal)
                    oud = sum(v for k,v in verd_totaal.items() if k in ["65-70","70-75","75-80","80-85","85-90","90+"])
                    jong= sum(v for k,v in verd_totaal.items() if k in ["0-5","5-10","10-15","15-20","20-25"])

                    a, b = st.columns(2)
                    a.metric("Inwoners", f"{int(tot):,}".replace(",","."))
                    b.metric("Gem. leeftijd", f"{gem:.1f} jaar" if gem else "—")
                    c, d_ = st.columns(2)
                    c.metric("Aandeel 65+", f"{oud/tot*100:.1f}%")
                    d_.metric("Aandeel 0-25", f"{jong/tot*100:.1f}%")

                    df_plot = pd.DataFrame([
                        {"Leeftijdsgroep": lbl, "Percentage": round(pct(verd_totaal).get(lbl,0),1)}
                        for lbl in LABELS_VOLGORDE
                    ])
                    fig = px.bar(df_plot, x="Leeftijdsgroep", y="Percentage",
                                 color_discrete_sequence=["#1D9E75"],
                                 labels={"Percentage":"%"}, height=260)
                    fig.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                                      xaxis=dict(tickangle=-45, tickfont=dict(size=9)),
                                      yaxis=dict(showgrid=True, gridcolor="#eee"),
                                      margin=dict(t=10, b=50, l=30, r=10),
                                      showlegend=False)
                    st.plotly_chart(fig, use_container_width=True)

                with dt2:
                    if not hh_totaal:
                        st.info("Geen huishoudensdata.")
                    else:
                        tot_hh = hh_totaal.get("__totaal", 1) or 1
                        st.metric("Totaal huishoudens", f"{int(tot_hh):,}".replace(",","."))
                        st.metric("Gem. grootte", f"{hh_totaal.get('__grootte',0):.1f} pers.")
                        pie_data = {k: v for k,v in hh_totaal.items() if not k.startswith("__")}
                        if pie_data:
                            fig_pie = px.pie(names=list(pie_data.keys()),
                                             values=list(pie_data.values()),
                                             color_discrete_sequence=["#1D9E75","#185FA5","#BA7517"],
                                             hole=0.45, height=250)
                            fig_pie.update_layout(margin=dict(t=10,b=10,l=10,r=10),
                                                  legend=dict(font=dict(size=11)))
                            st.plotly_chart(fig_pie, use_container_width=True)

                with dt3:
                    if not hk_totaal:
                        st.info("Geen herkomstdata.")
                    else:
                        tot_hk = hk_totaal.get("Totaal", 1) or 1
                        pct_nl = hk_totaal.get("Nederland",0)/tot_hk*100
                        st.metric("Herkomst Nederland", f"{pct_nl:.1f}%")
                        st.metric("Herkomst buiten NL",  f"{100-pct_nl:.1f}%")
                        hk_df = pd.DataFrame([
                            {"Herkomst": cat, "Percentage": round(hk_totaal.get(cat,0)/tot_hk*100,1)}
                            for cat in HK_CAT_ORDER if hk_totaal.get(cat,0) > 0
                        ])
                        if not hk_df.empty:
                            fig_hk = px.bar(hk_df, x="Percentage", y="Herkomst",
                                            orientation="h",
                                            color_discrete_sequence=["#534AB7"],
                                            labels={"Percentage":"%"}, height=280)
                            fig_hk.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                                                  xaxis=dict(showgrid=True, gridcolor="#eee"),
                                                  yaxis=dict(autorange="reversed"),
                                                  margin=dict(t=10, b=30, l=10, r=10),
                                                  showlegend=False)
                            st.plotly_chart(fig_hk, use_container_width=True)

st.divider()
st.caption("Data: CBS StatLine (CC BY 4.0) | Grenzen: cartomap.github.io | Geodata: PDOK")
