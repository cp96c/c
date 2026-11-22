#!/usr/bin/env python3
"""
Cruscotto convettivo attorno a LIRU - prototipo v0.1

Ogni esecuzione:
  1. calcola 17 punti (centro + 8 settori x 2 anelli a 25 e 50 NM)
  2. scarica da Open-Meteo CAPE, CIN, LI, zero termico, vento, raffiche, pioggia
  3. scarica i METAR dei campi vicini
  4. assegna livelli (regole semplici, NON probabilita' calibrate)
  5. scrive docs/index.html (la pagina per il telefono) e aggiunge una riga
     a data/history.jsonl (archivio per la futura calibrazione)

SUPPORTO INFORMATIVO: non sostituisce briefing, METAR/TAF/SIGMET, ATC
e osservazione diretta delle nubi.
"""
import datetime as dt
import html
import json
import math
import pathlib

import requests

# ---------------------------------------------------------------- configurazione
CENTER = (41.9519, 12.4994)          # LIRU
RINGS_NM = [25, 50]
SECTORS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
MODEL = "icon_eu"                    # prova anche "icon_seamless" o "ecmwf_ifs025"
HORIZON_H = 4                        # ore considerate (ora corrente + 3)
HOURLY = [
    "cape", "convective_inhibition", "lifted_index", "freezing_level_height",
    "wind_speed_10m", "wind_gusts_10m", "precipitation",
]
METAR_STATIONS = "LIRU,LIRF,LIRA,LIRE"
NM_KM = 1.852
LABELS = ["Basso", "Moderato", "Alto", "Molto alto"]

ROOT = pathlib.Path(__file__).parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"


# ---------------------------------------------------------------- geometria
def destination(lat, lon, bearing_deg, dist_nm):
    r = 6371.0
    d = dist_nm * NM_KM / r
    b = math.radians(bearing_deg)
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1),
                           math.cos(d) - math.sin(la1) * math.sin(la2))
    return round(math.degrees(la2), 4), round(math.degrees(lo2), 4)


def build_points():
    pts = [{"name": "LIRU", "sector": "C", "ring": 0, "lat": CENTER[0], "lon": CENTER[1]}]
    for ring in RINGS_NM:
        for sec, brg in SECTORS.items():
            lat, lon = destination(CENTER[0], CENTER[1], brg, ring)
            pts.append({"name": f"{sec} {ring}", "sector": sec, "ring": ring, "lat": lat, "lon": lon})
    return pts


# ---------------------------------------------------------------- dati
def fetch_model(points):
    params = {
        "latitude": ",".join(str(p["lat"]) for p in points),
        "longitude": ",".join(str(p["lon"]) for p in points),
        "hourly": ",".join(HOURLY),
        "models": MODEL,
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_hours": HORIZON_H + 2,
    }
    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"Open-Meteo errore {r.status_code}: {r.text[:300]}")
    data = r.json()
    return data if isinstance(data, list) else [data]


def fetch_metar():
    try:
        r = requests.get("https://aviationweather.gov/api/data/metar",
                         params={"ids": METAR_STATIONS, "format": "json", "hours": 2}, timeout=20)
        r.raise_for_status()
        latest = {}
        for m in r.json():
            icao = m.get("icaoId")
            if icao and (icao not in latest or m.get("obsTime", 0) > latest[icao].get("obsTime", 0)):
                latest[icao] = m
        return [(k, v.get("rawOb", "")) for k, v in sorted(latest.items())] or [("-", "nessun METAR ricevuto")]
    except Exception as e:  # noqa: BLE001
        return [("errore", str(e))]


# ---------------------------------------------------------------- regole (provvisorie)
def _vals(arr):
    return [v for v in (arr or [])[:HORIZON_H] if v is not None]


def _max(arr):
    v = _vals(arr)
    return max(v) if v else None


def _min(arr):
    v = _vals(arr)
    return min(v) if v else None


def conv_level(cape, cin, li):
    """0..3 oppure None. Ambiente favorevole alla convezione, non previsione di celle."""
    if cape is None:
        return None
    score = 3 if cape >= 2000 else 2 if cape >= 1000 else 1 if cape >= 300 else 0
    if li is not None and li <= -4 and score < 3:
        score += 1
    if cin is not None and cin >= 150 and score > 0:   # cappello forte: abbassa
        score -= 1
    return score


def wind_level(gust_kt):
    if gust_kt is None:
        return None
    return 0 if gust_kt < 20 else 1 if gust_kt < 30 else 2 if gust_kt < 40 else 3


def analyse(points, series):
    for p, s in zip(points, series):
        h = s.get("hourly", {})
        cin_abs = [abs(x) for x in _vals(h.get("convective_inhibition"))]
        p["cape"] = _max(h.get("cape"))
        p["cin"] = min(cin_abs) if cin_abs else None      # cappello piu' debole nelle ore considerate
        p["li"] = _min(h.get("lifted_index"))
        p["gust"] = _max(h.get("wind_gusts_10m"))
        p["wind"] = _max(h.get("wind_speed_10m"))
        p["precip"] = _max(h.get("precipitation"))
        fz = _vals(h.get("freezing_level_height"))
        p["frz"] = fz[0] if fz else None
        p["conv"] = conv_level(p["cape"], p["cin"], p["li"])
        p["windlvl"] = wind_level(p["gust"])
    return points


def regime(points):
    capes = [p["cape"] for p in points if p["cape"] is not None]
    if not capes:
        return "n/d (dati di instabilita' non disponibili)"
    if max(capes) >= 500:
        return "convettivo (instabilita' di massa d'aria)"
    return "poco convettivo: contano soprattutto vento e forzanti dinamiche (fronti, rilievi)"


# ---------------------------------------------------------------- pagina
CSS = """
:root{--bg:#f6f7f9;--fg:#15181c;--mut:#5b6470;--card:#fff;--bd:#d9dde3;
--c0:#cfe9d4;--c1:#fff1b3;--c2:#ffd0a0;--c3:#f5a3a3;--cn:#e3e5e8}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--fg:#e8eaed;--mut:#9aa3ad;--card:#1a1d21;--bd:#2c3137;
--c0:#24432b;--c1:#4d4519;--c2:#5a3a17;--c3:#5e2424;--cn:#2a2d31}}
*{box-sizing:border-box}
body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;max-width:640px;margin:auto}
h1{font-size:18px;margin:4px 0}h2{font-size:15px;margin:16px 0 6px}
.mut{color:var(--mut);font-size:13px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px;margin:8px 0}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th{font-size:13px;text-align:left;padding:4px}td{padding:3px;vertical-align:top}
.sec{font-weight:600;width:44px}
.b{border-radius:6px;padding:2px 6px;margin-bottom:2px;font-size:13px}
.c0{background:var(--c0)}.c1{background:var(--c1)}.c2{background:var(--c2)}.c3{background:var(--c3)}.cn{background:var(--cn)}
.s{font-size:12px;color:var(--mut)}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:2px 0}
.warn{border-left:4px solid #c0392b}
"""


def fmt(v, nd=0, unit=""):
    return "-" if v is None else f"{v:.{nd}f}{unit}"


def badge(prefix, lvl):
    if lvl is None:
        return f'<div class="b cn">{prefix}: n/d</div>'
    return f'<div class="b c{lvl}">{prefix}: {LABELS[lvl]}</div>'


def cell(p):
    return (f'<td>{badge("Conv", p["conv"])}{badge("Vento", p["windlvl"])}'
            f'<div class="s">CAPE {fmt(p["cape"])} · raff {fmt(p["gust"])} kt · pioggia {fmt(p["precip"], 1)}</div></td>')


def render(points, metars, now):
    center = points[0]
    by = {(p["sector"], p["ring"]): p for p in points[1:]}
    rows = "".join(
        f'<tr><td class="sec">{sec}</td>{cell(by[(sec, 25)])}{cell(by[(sec, 50)])}</tr>' for sec in SECTORS
    )
    ranked = sorted((p for p in points[1:] if p["conv"] is not None),
                    key=lambda p: (p["conv"], p["cape"] or 0), reverse=True)
    top = ""
    if ranked and ranked[0]["conv"] >= 1:
        t = ranked[0]
        top = (f'Settore piu\' favorevole alla convezione: <b>{t["name"]} NM</b> '
               f'({LABELS[t["conv"]]}, CAPE {fmt(t["cape"])} J/kg).')
    elif ranked:
        top = "Nessun settore con ambiente convettivo significativo nelle prossime ore."
    metar_html = "".join(f"<pre><b>{html.escape(k)}</b> {html.escape(v)}</pre>" for k, v in metars)
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Cruscotto LIRU</title><style>{CSS}</style></head><body>
<h1>Cruscotto convettivo · LIRU 50 NM</h1>
<div class="mut">Aggiornato {now:%d/%m %H:%M}Z · modello {MODEL} · orizzonte +{HORIZON_H - 1} h · prototipo v0.1</div>
<div class="card warn mut">Supporto informativo. Livelli a regole semplici, non probabilita'.
Non sostituisce briefing, METAR/TAF/SIGMET, ATC, osservazione diretta delle nubi.</div>
<div class="card"><b>Regime:</b> {html.escape(regime(points))}<br>{top}<br>
<span class="s">LIRU: CAPE {fmt(center["cape"])} J/kg · raffiche {fmt(center["gust"])} kt ·
zero termico ~{fmt(center["frz"])} m</span></div>
<h2>Settori (anello 25 NM | 50 NM)</h2>
<div class="card"><table><tr><th></th><th>25 NM</th><th>50 NM</th></tr>{rows}</table></div>
<div class="mut">Il modello descrive l'ambiente (griglia di alcuni km), non vede le singole celle.</div>
<h2>METAR</h2><div class="card">{metar_html}</div>
</body></html>"""


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(dt.timezone.utc)
    points = build_points()
    series = fetch_model(points)
    if len(series) != len(points):
        raise RuntimeError(f"Attesi {len(points)} punti, ricevuti {len(series)}")
    analyse(points, series)
    metars = fetch_metar()

    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(points, metars, now), encoding="utf-8")

    keep = ("name", "cape", "cin", "li", "gust", "wind", "precip", "frz", "conv", "windlvl")
    row = {"ts": now.isoformat(timespec="seconds"), "model": MODEL,
           "points": [{k: p[k] for k in keep} for p in points],
           "metar": dict(metars)}
    with open(DATA / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("OK", now.isoformat(timespec="seconds"), "regime:", regime(points))


if __name__ == "__main__":
    main()
