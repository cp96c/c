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
# Variabili opzionali: se una non e' disponibile la pagina funziona lo stesso
EXTRA = ["showers", "weather_code", "temperature_2m", "wind_speed_700hPa", "wind_direction_700hPa"]
TS_CAPE_MIN = 500      # J/kg: sotto questo valore non si segnala temporale
HAIL_CAPE_MIN = 1500   # J/kg: sopra, con pioggia forte, si segnala grandine possibile
# Scala di severita' crescente (numero = posizione). Ordine scelto da te, neve scelta da me.
RANK = {"-RA": 1, "-DZ": 2, "RA": 3, "-SN": 3, "DZ": 4, "+RA": 5, "SN": 5, "+SN": 6,
        "-SHRA": 6, "SHRA": 7, "SHSN": 7, "+SHRA": 8, "-TSRA": 9, "TSRA": 10, "+TSRA": 11, "TSGR": 12}
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
def fetch_model(points, hourly):
    params = {
        "latitude": ",".join(str(p["lat"]) for p in points),
        "longitude": ",".join(str(p["lon"]) for p in points),
        "hourly": ",".join(hourly),
        "models": MODEL,
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_hours": HORIZON_H + 3,
    }
    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"Open-Meteo errore {r.status_code}: {r.text[:300]}")
    data = r.json()
    return data if isinstance(data, list) else [data]


def _merge(series, extra):
    for base, ex in zip(series, extra):
        for k, v in ex.get("hourly", {}).items():
            if k != "time":
                base["hourly"][k] = v


def fetch_all(points):
    series = fetch_model(points, HOURLY)          # obbligatorio
    try:
        _merge(series, fetch_model(points, EXTRA))
    except RuntimeError:                          # una variabile non accettata: capisco quale (1 solo punto)
        ok = []
        for v in EXTRA:
            try:
                fetch_model(points[:1], [v])
                ok.append(v)
            except RuntimeError:
                pass
        if ok:
            _merge(series, fetch_model(points, ok))
    return series


def trim_past(series, now):
    """Open-Meteo parte dall'ora corrente: la prima voce puo' riferirsi a un'ora gia' finita. La scarto."""
    ref = now.strftime("%Y-%m-%dT%H:%M")
    for s in series:
        h = s.get("hourly", {})
        times = h.get("time", [])
        k = sum(1 for _ in __import__("itertools").takewhile(lambda t: t <= ref, times))
        if k:
            for key in list(h):
                if isinstance(h[key], list):
                    h[key] = h[key][k:]


def missing_vars(series):
    return [v for v in EXTRA if not any(_vals(s.get("hourly", {}).get(v)) for s in series)]


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


def to_nm(lat, lon):
    """Posizione in NM rispetto a LIRU: (x verso est, y verso nord)."""
    return ((lon - CENTER[1]) * math.cos(math.radians(CENTER[0])) * 60.0, (lat - CENTER[0]) * 60.0)


def eta_liru(p, max_miss_nm=10.0, max_min=120.0):
    """Minuti per raggiungere LIRU spostandosi in linea retta con il moto stimato; None se non ci passa."""
    if p["ring"] == 0 or not p["mkt"] or p["mkt"] < 5:
        return None
    m = math.radians(p["mdeg"])
    ux, uy = math.sin(m), math.cos(m)
    x, y = to_nm(p["lat"], p["lon"])
    along = -(x * ux + y * uy)                 # distanza lungo la traiettoria fino al punto piu' vicino a LIRU
    miss = abs(x * uy - y * ux)                # distanza minima da LIRU
    if along <= 0 or miss > max_miss_nm:
        return None
    mins = along / p["mkt"] * 60.0
    return mins if mins <= max_min else None


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


def _at(h, key, i):
    arr = h.get(key) or []
    return arr[i] if i < len(arr) else None


def _inten(x):
    return "-" if x < 2.5 else "" if x < 7.5 else "+"


def phenomenon(h, i):
    """Codice stile METAR dedotto dal modello per l'ora i. None = nessun fenomeno."""
    P, S = _at(h, "precipitation", i), _at(h, "showers", i)
    wc, cape, t2 = _at(h, "weather_code", i), _at(h, "cape", i), _at(h, "temperature_2m", i)
    wc = int(wc) if wc is not None else None
    if P is None and wc is None:
        return None
    P = P or 0.0
    ts_wc = wc in (95, 96, 99)
    shower = ts_wc or wc in (80, 81, 82, 85, 86) or (S is not None and P > 0 and S / P >= 0.5)
    if not shower and S is None and cape is not None and cape >= TS_CAPE_MIN and P >= 0.3:
        shower = True                              # senza dati sulla pioggia convettiva: stima dal CAPE
    if t2 is not None and t2 <= 1.0 and P >= 0.1 and not ts_wc:
        return "SHSN" if shower else ("-SN" if P < 1 else "SN" if P < 3 else "+SN")
    if ts_wc or (shower and cape is not None and cape >= TS_CAPE_MIN and P >= 0.3):
        if wc in (96, 99) or ((cape or 0) >= HAIL_CAPE_MIN and P >= 5):
            return "TSGR"
        return _inten(P) + "TSRA"
    if shower and P >= 0.1:
        return _inten(P) + "SHRA"
    if wc in (51, 53, 55, 56, 57):
        return "-DZ" if wc == 51 else "DZ"
    if P >= 0.1:
        return _inten(P) + "RA"
    return None


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
        codes = [phenomenon(h, i) for i in range(HORIZON_H)]
        ranked = [(RANK[c], c) for c in codes if c]
        p["ph"], p["ph_i"], p["ph_time"], p["motion"] = None, None, None, None
        p["mdeg"], p["mkt"], p["eta"], p["ph_ts"] = None, None, None, None
        if ranked:
            p["ph"] = max(ranked)[1]
            p["ph_i"] = codes.index(p["ph"])
            t = _at(h, "time", p["ph_i"])
            p["ph_time"] = (t[11:13] + "Z") if t else "?"
            p["ph_ts"] = t
            d, v = _at(h, "wind_direction_700hPa", p["ph_i"]), _at(h, "wind_speed_700hPa", p["ph_i"])
            if d is not None and v is not None:
                p["motion"] = (compass((d + 180) % 360), v)   # verso dove va, stimato dal vento a 700 hPa
                p["mdeg"], p["mkt"] = (d + 180) % 360, v
                p["eta"] = eta_liru(p)
        floor = {"-TSRA": 1, "TSRA": 1, "+TSRA": 2, "TSGR": 3}.get(p["ph"])   # Conv non sotto il fenomeno
        if floor is not None and (p["conv"] is None or p["conv"] < floor):
            p["conv"] = floor
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
--c0:#cfe9d4;--c1:#fff1b3;--c2:#ffd0a0;--c3:#f5a3a3;--cn:#e3e5e8;
--mapbg:#f3f1ea;--lake:#bcd7ee;--wedge:rgba(70,110,200,.12);
--m0:#4caf50;--m1:#e6c200;--m2:#f08a24;--m3:#d32f2f;--mn:#aab1b9}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--fg:#e8eaed;--mut:#9aa3ad;--card:#1a1d21;--bd:#2c3137;
--c0:#24432b;--c1:#4d4519;--c2:#5a3a17;--c3:#5e2424;--cn:#2a2d31;
--mapbg:#1b1d20;--lake:#1f3a52;--wedge:rgba(120,160,255,.14);
--m0:#4caf50;--m1:#e6c200;--m2:#f08a24;--m3:#ef5350;--mn:#6b737c}}
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
.stale{color:#c0392b;font-weight:700;font-size:14px}
.hd{display:flex;gap:8px;align-items:center;margin-bottom:6px}.sn{font-size:18px}
.sc{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}
.sc svg{flex:1 1 200px;max-width:270px;height:auto;border:1px solid var(--bd);border-radius:8px;background:var(--mapbg)}
.info{flex:1 1 190px}.ring{margin:0 0 9px}.ring .b{display:inline-block;margin-right:4px}
svg .coast{fill:none;stroke:var(--fg);stroke-width:1.3;opacity:.75}svg .lake{fill:var(--lake)}
svg .wedge{fill:var(--wedge);stroke:var(--mut);stroke-width:1}
svg .arc{fill:none;stroke:var(--mut);stroke-width:.8;stroke-dasharray:3 3}
svg .t{font-size:9px;fill:var(--fg);paint-order:stroke;stroke:var(--mapbg);stroke-width:2.5px}
svg .tb{font-size:10px;font-weight:700}svg .ap{fill:var(--mut)}svg .liru{fill:var(--fg)}
svg .pt{stroke:var(--fg);stroke-width:1.5}
svg .m0{fill:var(--m0)}svg .m1{fill:var(--m1)}svg .m2{fill:var(--m2)}svg .m3{fill:var(--m3)}svg .mn{fill:var(--mn)}
svg .arw{stroke:var(--fg);stroke-width:2.2;stroke-linecap:round}svg .ah{fill:var(--fg)}
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


AIRPORTS = {"LIRF": (41.800, 12.239), "LIRA": (41.799, 12.595), "LIRE": (41.654, 12.445),
            "LIRG": (41.994, 12.727), "LIRV": (42.430, 12.064), "LIRL": (41.542, 12.909)}
MAP_ALWAYS = False     # True = disegna la mappa anche per i settori senza fenomeni


def pheno_cls(code):
    if not code:
        return None
    rk = RANK[code]
    return 3 if rk >= 9 else 2 if rk >= 6 else 1 if rk >= 3 else 0


def _polar(r, brg):
    a = math.radians(brg)
    return (r * math.sin(a), r * math.cos(a))


def sector_map(sec, by):
    """Mappa schematica 64x64 NM del settore: costa, lago, anelli 25/50 NM, punti, frecce di moto."""
    brg, span = SECTORS[sec], 64.0
    k = 200.0 / span
    cx, cy = _polar(30, brg)
    x0, y1 = cx - span / 2, cy + span / 2

    def tp(x, y):
        return ((x - x0) * k, (y1 - y) * k)

    def pt(x, y):
        u, v = tp(x, y)
        return f"{u:.1f},{v:.1f}"

    def geo(q):
        return pt(*to_nm(q[1], q[0]))

    e = [f'<clipPath id="cl{sec}"><rect width="200" height="200"/></clipPath><g clip-path="url(#cl{sec})">']
    for ring in GEO["lakes"]:
        e.append(f'<polygon class="lake" points="{" ".join(geo(q) for q in ring)}"/>')
    for line in GEO["coast"]:
        e.append(f'<polyline class="coast" points="{" ".join(geo(q) for q in line)}"/>')
    arc = [brg - 22.5 + 4.5 * i for i in range(11)]
    e.append(f'<polygon class="wedge" points="{pt(0, 0)} {" ".join(pt(*_polar(50, a)) for a in arc)}"/>')
    for r in RINGS_NM:
        e.append(f'<polyline class="arc" points="{" ".join(pt(*_polar(r, a)) for a in arc)}"/>')
        lx, ly = tp(*_polar(r, brg + 17))
        e.append(f'<text class="t" x="{lx:.0f}" y="{ly:.0f}">{r}</text>')
    for icao, (la, lo) in AIRPORTS.items():
        ax, ay = tp(*to_nm(la, lo))
        if 6 < ax < 190 and 6 < ay < 194:
            e.append(f'<rect class="ap" x="{ax - 2:.0f}" y="{ay - 2:.0f}" width="4" height="4"/>'
                     f'<text class="t" x="{ax + 4:.0f}" y="{ay + 3:.0f}">{icao}</text>')
    ox, oy = tp(0, 0)
    e.append(f'<circle class="liru" cx="{ox:.0f}" cy="{oy:.0f}" r="4"/>'
             f'<text class="t tb" x="{ox + 6:.0f}" y="{oy - 5:.0f}">LIRU</text>')
    for r in RINGS_NM:
        p = by[(sec, r)]
        x, y = to_nm(p["lat"], p["lon"])
        u, v = tp(x, y)
        cls = pheno_cls(p["ph"])
        e.append(f'<circle class="pt m{"n" if cls is None else cls}" cx="{u:.1f}" cy="{v:.1f}" r="6"/>')
        if p["ph"]:
            e.append(f'<text class="t tb" text-anchor="middle" x="{u:.0f}" y="{v - 10:.0f}">{p["ph"]}</text>')
        if p["ph"] and p["mdeg"] is not None:
            m = math.radians(p["mdeg"])
            length = min(p["mkt"] / 2.0, 26.0)          # NM percorse in 30 minuti
            eu, ev = tp(x + length * math.sin(m), y + length * math.cos(m))
            while length > 6 and not (8 < eu < 192 and 8 < ev < 192):   # freccia dentro la mappa
                length *= 0.9
                eu, ev = tp(x + length * math.sin(m), y + length * math.cos(m))
            dx, dy = eu - u, ev - v
            n = math.hypot(dx, dy) or 1.0
            dx, dy = dx / n, dy / n
            bx, by_ = eu - 8 * dx, ev - 8 * dy
            e.append(f'<line class="arw" x1="{u:.1f}" y1="{v:.1f}" x2="{bx:.1f}" y2="{by_:.1f}"/>'
                     f'<polygon class="ah" points="{eu:.1f},{ev:.1f} {bx - 4 * dy:.1f},{by_ + 4 * dx:.1f} '
                     f'{bx + 4 * dy:.1f},{by_ - 4 * dx:.1f}"/>')
    e.append('</g><text class="t tb" x="6" y="13">N↑</text>')
    return f'<svg viewBox="0 0 200 200" role="img" aria-label="Mappa settore {sec}">{"".join(e)}</svg>'


def arrival_text(p, now):
    """Ora stimata di arrivo su LIRU. Il dato orario e' la media dell'ora che finisce a ph_ts:
    uso il centro di quell'ora (mezz'ora prima) piu' il tempo di percorrenza, con +-30'."""
    if not p.get("ph_ts") or p["eta"] is None:
        return ""
    t0 = dt.datetime.strptime(p["ph_ts"], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc)
    arr = t0 - dt.timedelta(minutes=30) + dt.timedelta(minutes=p["eta"])
    if arr <= now:
        return "imminente"
    arr = arr.replace(second=0, microsecond=0)
    arr += dt.timedelta(minutes=(-arr.minute) % 5)          # arrotondo ai 5 minuti
    return f"~{arr:%H:%M}Z ±30'"


def sector_card(sec, by, now):
    pts = [by[(sec, r)] for r in RINGS_NM]
    worst = max(pts, key=lambda p: RANK.get(p["ph"], 0))
    has = worst["ph"] is not None
    if has:
        head = (f'<div class="hd"><b class="sn">{sec}</b>'
                f'<span class="b c{pheno_cls(worst["ph"])}"><b>{worst["ph"]}</b></span>'
                f'<span class="s">{worst["ring"]} NM, entro le {worst["ph_time"]}</span></div>')
    else:
        head = f'<div class="hd"><b class="sn">{sec}</b><span class="s">nessun fenomeno nel modello</span></div>'
    rings = ""
    for p in pts:
        if p["ph"]:
            line = f'<span class="b c{pheno_cls(p["ph"])}"><b>{p["ph"]}</b></span> entro le {p["ph_time"]}'
            if p["motion"]:
                line += f' · verso {p["motion"][0]} {p["motion"][1]:.0f} kt'
            if p["eta"] is not None:
                line += f' · su LIRU {arrival_text(p, now)}'
        else:
            line = '<span class="s">nessun fenomeno</span>'
        rings += (f'<div class="ring"><b>{p["ring"]} NM</b> {line}<br>'
                  f'{badge("Conv", p["conv"])}{badge("Vento", p["windlvl"])}'
                  f'<div class="s">CAPE {fmt(p["cape"])} · raff {fmt(p["gust"])} kt · pioggia {fmt(p["precip"], 1)} mm/h</div></div>')
    svg = sector_map(sec, by) if (has or MAP_ALWAYS) else ""
    return f'<div class="card">{head}<div class="sc">{svg}<div class="info">{rings}</div></div></div>'


def missing_note(missing):
    if not missing:
        return ""
    n = f'<div class="s">Dati non disponibili dal modello: {", ".join(missing)}.'
    if "weather_code" in missing:
        n += " DZ non stimabile."
    if "showers" in missing:
        n += " Distinzione rovescio/pioggia stimata dal CAPE."
    if "wind_speed_700hPa" in missing or "wind_direction_700hPa" in missing:
        n += " Moto non disponibile."
    return n + "</div>"


def render(points, metars, now, missing=()):
    center = points[0]
    by = {(p["sector"], p["ring"]): p for p in points[1:]}
    ranked = sorted((p for p in points[1:] if p["conv"] is not None),
                    key=lambda p: (p["conv"], p["cape"] or 0), reverse=True)
    top = ""
    if ranked and ranked[0]["conv"] >= 1:
        t = ranked[0]
        top = (f'Ambiente più favorevole alla convezione: <b>{t["name"]} NM</b> '
               f'({LABELS[t["conv"]]}, CAPE {fmt(t["cape"])} J/kg).')
    elif ranked:
        top = "Nessun settore con ambiente convettivo significativo nelle prossime ore."
    liru_ph = f'<b>{center["ph"]}</b> entro le {center["ph_time"]}' if center["ph"] else "nessun fenomeno"
    order = sorted(SECTORS, key=lambda sc: (-max(RANK.get(by[(sc, r)]["ph"], 0) for r in RINGS_NM), list(SECTORS).index(sc)))
    cards = "".join(sector_card(sc, by, now) for sc in order)
    age_js = ('<div id="age" class="mut"></div><script>(function(){var t=EPOCH*1000;function u(){'
              'var m=Math.round((Date.now()-t)/60000),e=document.getElementById("age");'
              'if(m>30){e.className="stale";e.textContent="ATTENZIONE: dati vecchi di "+m+" min. '
              'GitHub ha ritardato l\'aggiornamento: non fidarti di questa pagina.";}'
              'else{e.className="mut";e.textContent="Dati generati "+m+" min fa";}}'
              'u();setInterval(u,20000);})();</script>').replace("EPOCH", str(int(now.timestamp())))
    metar_html = "".join(f"<pre><b>{html.escape(k)}</b> {html.escape(v)}</pre>" for k, v in metars)
    legend = ("Scala di severità crescente: -RA, -DZ, RA, DZ, +RA, -SHRA, SHRA, +SHRA, -TSRA, TSRA, +TSRA, TSGR "
              "(neve: -SN, SN, +SN, SHSN). Freccia = direzione di spostamento (vento a 700 hPa; più lunga = più veloce, accorciata ai bordi della mappa). "
              "«Su LIRU» = ora stimata di arrivo, estrapolazione in linea retta con incertezza di ±30' (dati orari). Conv non è mai inferiore al fenomeno previsto (TSRA almeno Moderato). "
              "Le mappe dei settori senza fenomeni sono omesse.")
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Cruscotto LIRU</title><style>{CSS}</style></head><body>
<h1>Cruscotto convettivo · LIRU 50 NM</h1>
<div class="mut">Aggiornato {now:%d/%m %H:%M}Z · modello {MODEL} · orizzonte +{HORIZON_H - 1} h · prototipo v0.4</div>
{age_js}
<div class="card warn mut">Supporto informativo. Livelli a regole semplici, non probabilità.
Non sostituisce briefing, METAR/TAF/SIGMET, ATC, osservazione diretta delle nubi.</div>
<div class="card"><b>Regime:</b> {html.escape(regime(points))}<br>{top}<br>
<b>LIRU:</b> {liru_ph} · <span class="s">CAPE {fmt(center["cape"])} J/kg · raffiche {fmt(center["gust"])} kt ·
zero termico ~{fmt(center["frz"])} m</span>{missing_note(missing)}</div>
<h2>Settori, dal più severo</h2>
{cards}
<div class="mut">{legend}</div>
<h2>METAR</h2><div class="card">{metar_html}</div>
</body></html>"""


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(dt.timezone.utc)
    points = build_points()
    series = fetch_all(points)
    if len(series) != len(points):
        raise RuntimeError(f"Attesi {len(points)} punti, ricevuti {len(series)}")
    trim_past(series, now)
    analyse(points, series)
    missing = missing_vars(series)
    metars = fetch_metar()

    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(points, metars, now, missing), encoding="utf-8")

    keep = ("name", "cape", "cin", "li", "gust", "wind", "precip", "frz", "conv", "windlvl", "ph", "ph_i", "mdeg", "mkt", "eta")
    row = {"ts": now.isoformat(timespec="seconds"), "model": MODEL, "missing": missing,
           "points": [{k: p[k] for k in keep} for p in points],
           "metar": dict(metars)}
    with open(DATA / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("OK", now.isoformat(timespec="seconds"), "regime:", regime(points))


# Costa e Lago di Bracciano (Natural Earth, semplificati): [lon, lat]
GEO = {"coast":[[[13.849,43.092],[13.865,43.007],[13.945,42.798],[14.001,42.698],[14.063,42.625],[14.075,42.599]],[[14.096,40.834],[14.083,40.831],[14.076,40.822],[14.077,40.81]],[[14.052,40.838],[14.044,40.874],[14.021,40.922],[13.965,40.996],[13.918,41.024],[13.911,41.065],[13.852,41.147],[13.79,41.206],[13.722,41.252],[13.709,41.256],[13.663,41.251],[13.657,41.26],[13.594,41.253],[13.564,41.238],[13.575,41.208],[13.544,41.207],[13.534,41.208],[13.514,41.23],[13.5,41.222],[13.48,41.239],[13.324,41.295],[13.286,41.296],[13.187,41.278],[13.151,41.26],[13.115,41.251],[13.091,41.227],[13.068,41.222],[13.045,41.228],[13.024,41.274],[12.98,41.331],[12.894,41.399],[12.842,41.418],[12.749,41.424],[12.654,41.466],[12.635,41.447],[12.546,41.544],[12.448,41.631],[12.327,41.711],[12.239,41.736],[12.223,41.751],[12.215,41.768],[12.206,41.827],[12.147,41.903],[12.039,41.965],[12.02,41.993],[11.982,42.0],[11.935,42.031],[11.917,42.038],[11.827,42.034],[11.809,42.066],[11.75,42.128],[11.736,42.169],[11.658,42.279],[11.539,42.349],[11.489,42.359],[11.396,42.393],[11.396,42.4],[11.377,42.407],[11.355,42.407],[11.362,42.4],[11.333,42.401],[11.259,42.421],[11.211,42.419],[11.184,42.366],[11.156,42.366],[11.135,42.384],[11.107,42.391],[11.094,42.403],[11.087,42.429],[11.099,42.443],[11.113,42.446],[11.164,42.44],[11.18,42.456],[11.188,42.48],[11.187,42.521],[11.158,42.564],[11.129,42.558],[11.097,42.607],[11.081,42.613],[11.078,42.632],[11.006,42.668],[10.99,42.71],[10.944,42.743]],[[10.909,42.325],[10.923,42.319],[10.94,42.331],[10.913,42.375]],[[13.432,40.801],[13.424,40.8]]],"lakes":[[[11.977,42.64],[12.0,42.62],[12.0,42.613],[11.993,42.6],[11.995,42.561],[11.985,42.544],[11.964,42.536],[11.941,42.537],[11.92,42.546],[11.87,42.579],[11.859,42.593],[11.862,42.613],[11.875,42.632],[11.894,42.643],[11.945,42.653],[11.977,42.64]]]}


if __name__ == "__main__":
    main()
