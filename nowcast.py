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
import os
import re
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
EXTRA = ["showers", "weather_code", "temperature_2m", "wind_speed_700hPa", "wind_direction_700hPa",
         "wind_direction_10m", "dew_point_2m", "cloud_cover_low", "pressure_msl"]
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
    try:                                              # nubi medie e alte: solo per LIRU (1 punto)
        _merge(series[:1], fetch_model(points[:1], ["cloud_cover_mid", "cloud_cover_high"]))
    except RuntimeError:
        pass
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


GRID_N, GRID_REFRESH_MIN = 17, 100
GRID_VARS = ["precipitation", "showers", "weather_code", "cape", "temperature_2m", "wind_speed_700hPa", "wind_direction_700hPa"]
PALETTE = []


def grid_points():
    cs = math.cos(math.radians(CENTER[0]))
    return [(round(CENTER[0] + (-50 + 100 * j / (GRID_N - 1)) / 60, 4),
             round(CENTER[1] + (-50 + 100 * i / (GRID_N - 1)) / (60 * cs), 4))
            for j in range(GRID_N) for i in range(GRID_N)]       # da sud a nord, da ovest a est


def fetch_grid(now):
    path, old = DOCS / "grid.json", None
    try:
        if path.exists():
            old = json.loads(path.read_text())
            if old.get("n") == GRID_N and "v" in old and (now - dt.datetime.fromisoformat(old["ts"])).total_seconds() < GRID_REFRESH_MIN * 60:
                return old
            if old.get("n") != GRID_N or "v" not in old:
                old = None
        pts, times, cols = grid_points(), None, {v: [] for v in GRID_VARS}
        for k in range(0, len(pts), 45):
            ch = pts[k:k + 45]
            r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=40, params={
                "latitude": ",".join(str(a) for a, _ in ch), "longitude": ",".join(str(b) for _, b in ch),
                "hourly": ",".join(GRID_VARS), "models": MODEL, "wind_speed_unit": "kn", "timezone": "UTC",
                "forecast_hours": HORIZON_H + 3})
            if r.status_code != 200:
                raise RuntimeError(f"Open-Meteo {r.status_code}: {r.text[:200]}")
            d = r.json()
            for loc in (d if isinstance(d, list) else [d]):
                times = times or loc["hourly"]["time"]
                for v in GRID_VARS:
                    cols[v].append(loc["hourly"].get(v) or [None] * len(times))
        nd = {"precipitation": 2, "showers": 2, "temperature_2m": 1}
        rd = lambda x, v: None if x is None else round(x, nd.get(v, 0))
        g = {"n": GRID_N, "ts": now.isoformat(timespec="seconds"), "t": times,
             "v": {v: [[rd(cols[v][c][h], v) for c in range(len(pts))] for h in range(len(times))] for v in GRID_VARS}}
        DOCS.mkdir(exist_ok=True)
        path.write_text(json.dumps(g, separators=(",", ":")))
        return g
    except Exception as e:  # noqa: BLE001
        print("griglia: non riuscita:", e)
        return old


def _hull(pts):
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def _extent(xs, ys, step):
    n = len(xs)
    if n == 1:
        return step, step
    mx, my = sum(xs) / n, sum(ys) / n
    th = 0.5 * math.atan2(2 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n,
                          sum((x - mx) ** 2 for x in xs) / n - sum((y - my) ** 2 for y in ys) / n)
    c, s_ = math.cos(th), math.sin(th)
    a = [(x - mx) * c + (y - my) * s_ for x, y in zip(xs, ys)]
    b = [-(x - mx) * s_ + (y - my) * c for x, y in zip(xs, ys)]
    return tuple(sorted((max(a) - min(a) + step, max(b) - min(b) + step), reverse=True))


def grid_analysis(g, now):
    """Dalla griglia: codice del fenomeno in ogni cella, figure (ammassi di celle) con dimensione, moto e durata stimati."""
    if not g or "v" not in g:
        return None
    ref = now.strftime("%Y-%m-%dT%H:%M")
    ti = [i for i, t in enumerate(g["t"]) if t > ref][:HORIZON_H]
    if not ti:
        return None
    N, V, T = g["n"], g["v"], len(ti)
    pts = grid_points()
    cs = math.cos(math.radians(CENTER[0]))
    xy = [((lo - CENTER[1]) * cs * 60, (la - CENTER[0]) * 60) for la, lo in pts]
    step = 100.0 / (N - 1)
    codes = []
    for t in ti:
        row = []
        for c in range(N * N):
            if math.hypot(*xy[c]) > 53:
                row.append(None)
                continue
            h = {k: [V[k][t][c]] for k in ("precipitation", "showers", "weather_code", "cape", "temperature_2m")}
            row.append(phenomenon(h, 0))
        codes.append(row)
    dur = [[0] * (N * N) for _ in range(T)]
    for t in range(T - 1, -1, -1):
        for c in range(N * N):
            dur[t][c] = ((dur[t + 1][c] if t + 1 < T else 0) + 1) if codes[t][c] else 0
    to_ll = lambda x, y: [round(CENTER[0] + y / 60, 4), round(CENTER[1] + x / (60 * cs), 4)]
    clusters = []
    for t in range(T):
        seen, hour = set(), []
        for c0 in range(N * N):
            if not codes[t][c0] or c0 in seen:
                continue
            k0 = pheno_cls(codes[t][c0])
            comp, stack = [], [c0]
            seen.add(c0)
            while stack:
                q = stack.pop()
                comp.append(q)
                j, i = divmod(q, N)
                for dj in (-1, 0, 1):
                    for di in (-1, 0, 1):
                        jj, ii = j + dj, i + di
                        qq = jj * N + ii
                        if 0 <= jj < N and 0 <= ii < N and qq not in seen and codes[t][qq] and pheno_cls(codes[t][qq]) == k0:
                            seen.add(qq)
                            stack.append(qq)
            xs, ys = [xy[q][0] for q in comp], [xy[q][1] for q in comp]
            code = max((codes[t][q] for q in comp), key=lambda c: RANK[c])
            L_, W_ = _extent(xs, ys, step)
            u = v = nw = 0.0
            for q in comp:
                d_, k_ = V["wind_direction_700hPa"][ti[t]][q], V["wind_speed_700hPa"][ti[t]][q]
                if d_ is not None and k_ is not None:
                    u += k_ * math.sin(math.radians(d_ + 180))
                    v += k_ * math.cos(math.radians(d_ + 180))
                    nw += 1
            m_ = v_ = None
            if nw:
                v_ = round(math.hypot(u, v) / nw)
                m_ = round(math.degrees(math.atan2(u, v)) % 360)
            r_ = step / 2 * 1.15
            corners = [(xy[q][0] + r_ * math.cos(math.radians(a)), xy[q][1] + r_ * math.sin(math.radians(a))) for q in comp for a in range(0, 360, 45)]
            hour.append({"x": code, "k": k0, "p": [to_ll(*p) for p in _hull(corners)], "c": to_ll(sum(xs) / len(xs), sum(ys) / len(ys)),
                         "L": round(L_), "W": round(W_), "m": m_, "v": v_, "n": len(comp),
                         "d": max(1, round(sum(dur[t][q] for q in comp) / len(comp))), "o": int(any(t + dur[t][q] >= T for q in comp)),
                         "lb": int(RANK[code] >= 3 or len(comp) >= 3)})
        clusters.append(hour)
    sec = {}
    for name, b in SECTORS.items():
        sec[name] = []
        for t in range(T):
            best = None
            for c in range(N * N):
                x, y = xy[c]
                d = math.hypot(x, y)
                if codes[t][c] and 1 < d <= 50:
                    diff = ((math.degrees(math.atan2(x, y)) % 360 - b + 180) % 360) - 180
                    if abs(diff) <= 22.5 and (best is None or RANK[codes[t][c]] > RANK[best]):
                        best = codes[t][c]
            sec[name].append(best)
    return {"t": [g["t"][i] for i in ti], "cl": clusters, "sec": sec}


def apply_grid(points, ga):
    """Il fenomeno peggiore del settore tiene conto anche della griglia all'interno dei cerchi."""
    if not ga:
        return
    by = {(p["sector"], p["ring"]): p for p in points[1:]}
    for sec in SECTORS:
        for h in by[(sec, RINGS_NM[0])]["hrs"]:
            if h["t"] in ga["t"]:
                h["gw"] = ga["sec"][sec][ga["t"].index(h["t"])]


def grid_for_page(g, now):
    if not g:
        return None
    ref = now.strftime("%Y-%m-%dT%H:%M")
    idx = [i for i, t in enumerate(g["t"]) if t > ref][:HORIZON_H]
    if not idx:
        return None
    la, lo = 50 / 60, 50 / (60 * math.cos(math.radians(CENTER[0])))
    return {"n": g["n"], "t": [g["t"][i] for i in idx], "p": [g["p"][i] for i in idx], "c": [g["c"][i] for i in idx],
            "b": [[CENTER[0] - la, CENTER[1] - lo], [CENTER[0] + la, CENTER[1] + lo]]}


def cached(name, max_min, fn, now):
    """Rilegge docs/<name>.json se piu' recente di max_min minuti, altrimenti lo rigenera (e in caso di errore usa la copia vecchia)."""
    path, old = DOCS / f"{name}.json", None
    try:
        if path.exists():
            old = json.loads(path.read_text())
            if (now - dt.datetime.fromisoformat(old["ts"])).total_seconds() < max_min * 60:
                return old["data"]
        data = fn()
        DOCS.mkdir(exist_ok=True)
        path.write_text(json.dumps({"ts": now.isoformat(timespec="seconds"), "data": data}, separators=(",", ":")))
        return data
    except Exception as e:  # noqa: BLE001
        print(f"{name}: aggiornamento non riuscito ({e})")
        if old:
            return old["data"]
        raise


def _rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1e-12
    dm, ix = 0, 0
    for i in range(1, len(pts) - 1):
        d = abs(dy * pts[i][0] - dx * pts[i][1] + x2 * y1 - y2 * x1) / L
        if d > dm:
            dm, ix = d, i
    return _rdp(pts[:ix + 1], eps)[:-1] + _rdp(pts[ix:], eps) if dm > eps else [pts[0], pts[-1]]


def fetch_airspace():
    """Spazi aerei da OpenAIP (serve OPENAIP_API_KEY). Solo contorni, indicativi."""
    key = os.environ.get("OPENAIP_API_KEY")
    if not key:
        return None
    la, lo = 55 / 60, 55 / (60 * math.cos(math.radians(CENTER[0])))
    bb = (CENTER[1] - lo, CENTER[0] - la, CENTER[1] + lo, CENTER[0] + la)
    items, page = [], 1
    while page <= 4:
        r = requests.get("https://api.core.openaip.net/api/airspaces", timeout=60, headers={"x-openaip-api-key": key},
                         params={"bbox": ",".join(f"{v:.3f}" for v in bb), "limit": 500, "page": page})
        r.raise_for_status()
        d = r.json()
        items += d.get("items", [])
        if page >= d.get("totalPages", 1):
            break
        page += 1
    out = []
    for a in items:
        if a.get("type") in (10, 11, 12, 15, 16):            # FIR/UIR/ADIZ/aerovie: troppo grandi, tolte
            continue
        g = a.get("geometry") or {}
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
        rings = []
        for p in polys:
            ring = [(x[0], x[1]) for x in p[0]]
            if any(bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] for x, y in ring):
                rings.append([[round(x, 3), round(y, 3)] for x, y in _rdp(ring, 0.002)])
        if rings:
            out.append({"n": a.get("name", "?"), "t": a.get("type"), "r": rings})
    print("spazi aerei:", len(out))
    return out


AIRPORT_MENU = {"LIRZ": (43.096, 12.513), "LIBP": (42.432, 14.181), "LIPY": (43.616, 13.362), "LIRA": (41.799, 12.595),
                "LIRF": (41.800, 12.239), "LIQL": (43.825, 10.578), "LIPE": (44.535, 11.289), "LIAP": (42.370, 13.309)}
AP_OPTS = "".join(f'<option value="{k}">{k}</option>' for k in AIRPORT_MENU)
ITALY_BB = (6.0, 35.3, 19.0, 47.3)
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"


def _clip_poly(pts, bb):
    """Sutherland-Hodgman: ritaglia un poligono sul rettangolo bb=(x0,y0,x1,y1)."""
    for axis, val, ge in ((0, bb[0], True), (0, bb[2], False), (1, bb[1], True), (1, bb[3], False)):
        out = []
        for a, b in zip(pts, pts[1:] + pts[:1]):
            ina, inb = (a[axis] >= val) == ge or a[axis] == val, (b[axis] >= val) == ge or b[axis] == val
            if ina != inb:
                t = (val - a[axis]) / (b[axis] - a[axis])
                out.append((val, a[1] + t * (b[1] - a[1])) if axis == 0 else (a[0] + t * (b[0] - a[0]), val))
            if inb:
                out.append(b)
        pts = out
        if not pts:
            break
    return pts


def _rdp_ring(ring, eps):
    h = len(ring) // 2
    return _rdp(ring[:h + 1], eps)[:-1] + _rdp(ring[h:] + ring[:1], eps)[:-1]


def fetch_italy():
    """Costa, confini, terre emerse e laghi (Natural Earth) per l'Italia intera. Si scarica una volta sola."""
    import sys
    sys.setrecursionlimit(30000)
    inb = lambda x, y: ITALY_BB[0] <= x <= ITALY_BB[2] and ITALY_BB[1] <= y <= ITALY_BB[3]
    out = {}
    for key, fn in (("coast", "ne_10m_coastline"), ("borders", "ne_10m_admin_0_boundary_lines_land")):
        r = requests.get(NE_URL + fn + ".geojson", timeout=180)
        r.raise_for_status()
        lines = []
        for f in r.json()["features"]:
            g = f["geometry"]
            for line in (g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]):
                cur = []
                for pt in line:
                    if inb(pt[0], pt[1]):
                        cur.append((pt[0], pt[1]))
                    else:
                        if len(cur) > 1:
                            lines.append(cur)
                        cur = []
                if len(cur) > 1:
                    lines.append(cur)
        out[key] = [[[round(x, 3), round(y, 3)] for x, y in _rdp(l, 0.004)] for l in lines]
    for key, fn in (("land", "ne_10m_land"), ("lakes", "ne_10m_lakes")):
        r = requests.get(NE_URL + fn + ".geojson", timeout=300)
        r.raise_for_status()
        rings = []
        for f in r.json()["features"]:
            g = f["geometry"]
            for poly in (g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]):
                ring = [(p[0], p[1]) for p in poly[0]]
                if ring and ring[0] == ring[-1]:
                    ring = ring[:-1]
                if len(ring) < 3 or (key == "lakes" and not any(inb(x, y) for x, y in ring)):
                    continue
                c = _clip_poly(ring, ITALY_BB)
                if len(c) >= 3:
                    c = _rdp_ring(c, 0.006)
                    if len(c) >= 3:
                        rings.append([[round(x, 3), round(y, 3)] for x, y in c])
        out[key] = rings
    return out


def fetch_airports():
    """METAR e TAF ufficiali dei campi del menu (aviationweather.gov)."""
    out = {k: {"lat": v[0], "lon": v[1], "metar": "", "taf": ""} for k, v in AIRPORT_MENU.items()}
    ids = ",".join(AIRPORT_MENU)
    for kind, key, raw, stamp in (("metar", "metar", "rawOb", "obsTime"), ("taf", "taf", "rawTAF", "issueTime")):
        try:
            r = requests.get(f"https://aviationweather.gov/api/data/{kind}", params={"ids": ids, "format": "json", "hours": 4}, timeout=20)
            best = {}
            for m in r.json():
                c = m.get("icaoId")
                if c in out and m.get(raw) and str(m.get(stamp, "")) >= str(best.get(c, ("", ""))[0]):
                    best[c] = (m.get(stamp, ""), m[raw])
            for c, (_, txt) in best.items():
                out[c][key] = txt
        except Exception as e:  # noqa: BLE001
            print(f"aeroporti {kind}: {e}")
    return out


def missing_vars(series):
    return [v for v in EXTRA if not any(_vals(s.get("hourly", {}).get(v)) for s in series)]


WMS = "https://view.eumetsat.int/geoserver/wms"
SAT_LAYERS = {"geocolour": "mtg_fd:rgb_geocolour", "ir": "mtg_fd:ir105_hrfi"}


def _last_times(text, n=7):
    parse = lambda x: dt.datetime.strptime(x.strip().replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S")
    fmt_ = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    last = text.split(",")[-1].strip()
    if "/" in last:                                   # inizio/fine/periodo, es. .../PT10M
        _, end, step = last.split("/")
        mins = int(step[2:-1]) if step.startswith("PT") and step.endswith("M") else 10
        return [fmt_(parse(end) - dt.timedelta(minutes=mins * i)) for i in reversed(range(n))]
    return [fmt_(parse(x)) for x in text.split(",")][-n:]


def sat_times():
    out = {k: [] for k in SAT_LAYERS}
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(WMS, params={"service": "WMS", "request": "GetCapabilities", "version": "1.3.0"}, timeout=60)
        r.raise_for_status()
        for el in ET.fromstring(r.content).iter():
            if el.tag.split("}")[-1] != "Layer":
                continue
            kids = {c.tag.split("}")[-1]: c for c in el if c.tag.split("}")[-1] in ("Name", "Dimension")}
            name = kids["Name"].text if "Name" in kids else None
            for key, lay in SAT_LAYERS.items():
                d = kids.get("Dimension")
                if name == lay and d is not None and d.text and d.get("name", "").lower() == "time":
                    out[key] = _last_times(d.text)
    except Exception as e:  # noqa: BLE001
        print("satellite: GetCapabilities non riuscito:", e)
    return out


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
        p["hrs"] = []                                   # dettaglio ora per ora (immagine prevista e TAF)
        for i in range(HORIZON_H):
            d, v = _at(h, "wind_direction_700hPa", i), _at(h, "wind_speed_700hPa", i)
            p["hrs"].append({"t": _at(h, "time", i), "ph": codes[i], "m": (d + 180) % 360 if d is not None else None, "k": v,
                             "wd": _at(h, "wind_direction_10m", i), "ws": _at(h, "wind_speed_10m", i),
                             "wg": _at(h, "wind_gusts_10m", i), "T": _at(h, "temperature_2m", i),
                             "Td": _at(h, "dew_point_2m", i), "cl": _at(h, "cloud_cover_low", i),
                             "P": _at(h, "pressure_msl", i),
                             "cm": _at(h, "cloud_cover_mid", i), "ch": _at(h, "cloud_cover_high", i)})
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
#map{width:100%;aspect-ratio:1;max-height:82vh;border-radius:8px}
.sl{font-weight:700;font-size:11px;text-align:center;line-height:1.15;color:var(--fg);text-shadow:1px 0 var(--mapbg),-1px 0 var(--mapbg),0 1px var(--mapbg),0 -1px var(--mapbg),1px 1px var(--mapbg),-1px -1px var(--mapbg),1px -1px var(--mapbg),-1px 1px var(--mapbg);pointer-events:none}
.compass{display:grid;grid-template-columns:minmax(64px,1fr) minmax(0,3.2fr) minmax(64px,1fr);gap:4px}
.compass #map{height:auto;aspect-ratio:1;min-height:190px}
.cb{border-radius:8px;padding:4px 5px;font-size:11px;line-height:1.3;min-height:70px;overflow:hidden}
.cb b{font-size:13px}.cb .cd{font-weight:700;font-size:12px}
.leaflet-container .leaflet-tooltip{background:none!important;border:0!important;box-shadow:none!important;color:var(--fg)!important;font-size:10px;font-weight:700;padding:0!important;text-shadow:1px 0 var(--mapbg),-1px 0 var(--mapbg),0 1px var(--mapbg),0 -1px var(--mapbg),1px 1px var(--mapbg),-1px -1px var(--mapbg),1px -1px var(--mapbg),-1px 1px var(--mapbg)!important}
.leaflet-container .leaflet-tooltip:before{display:none!important}
.leaflet-rad-pane img,.leaflet-sat-pane img{image-rendering:auto!important}
.pl{font-weight:700;font-size:11px;text-align:center;color:var(--fg);text-shadow:1px 0 var(--mapbg),-1px 0 var(--mapbg),0 1px var(--mapbg),0 -1px var(--mapbg),1px 1px var(--mapbg),-1px -1px var(--mapbg),1px -1px var(--mapbg),-1px 1px var(--mapbg)}
.ar{font-size:16px;line-height:16px;text-align:center;color:var(--fg);text-shadow:1px 0 var(--mapbg),-1px 0 var(--mapbg),0 1px var(--mapbg),0 -1px var(--mapbg),1px 1px var(--mapbg),-1px -1px var(--mapbg),1px -1px var(--mapbg),-1px 1px var(--mapbg)}
.far .loc{display:none}
.cb{cursor:pointer}.taf{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:6px;font-size:12px;white-space:pre-wrap}
.hb{font-size:13px;padding:3px 9px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}.hb.on{background:var(--c1);font-weight:700}
.chip{display:inline-block;width:14px;height:14px;border-radius:50%;font-size:9px;text-align:center;line-height:14px;margin:2px 2px 0 0;color:#000}
.k0{background:var(--m0)}.k1{background:var(--m1)}.k2{background:var(--m2)}.k3{background:var(--m3)}.kn{background:var(--mn)}
summary{cursor:pointer;font-weight:600;margin:12px 0 6px}.ctl{display:flex;gap:8px;align-items:center;margin-top:6px}
.ctl input[type=range]{flex:1}.ctl button{font-size:18px;padding:2px 10px}.ctl select{font-size:14px}
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


CSS += """
body{font-size:16px;max-width:780px}h1{font-size:23px;margin:2px 0}h2{font-size:17px;margin:18px 0 6px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin:4px 0 8px}
#age{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--cn);white-space:nowrap}
#age.stale{background:var(--c3);color:var(--fg);font-weight:700}
.card{border-radius:14px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.18)}.mapcard{padding:8px}
#map{border-radius:10px;box-shadow:inset 0 0 0 1px var(--bd)}
.bar{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}.bar input[type=range]{flex:1;min-width:110px}
.btn{font-size:18px;padding:3px 14px;border-radius:999px;border:1px solid var(--bd);background:var(--card);color:var(--fg)}
.opt{display:flex;align-items:center;gap:4px;font-size:14px}
select{font-size:14px;padding:4px 8px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--fg)}
#tl{min-width:150px;text-align:right}#tl.fc{color:#f08a24}
.lgd{display:inline-block;padding:1px 8px;border-radius:999px;color:#111;font-size:12px}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 8px}
.tile{flex:1 1 105px;border-radius:12px;padding:8px 10px;line-height:1.1}
.tile .tl{display:block;font-size:11px;letter-spacing:.5px;text-transform:uppercase;opacity:.7;margin-bottom:4px}
.tile b{font-size:22px}.tile .tu{font-size:12px;opacity:.7}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:4px 0 6px}.chips .b{display:inline-block;margin:0;font-size:13px}
.lab{font-size:12px;letter-spacing:.5px;text-transform:uppercase;color:var(--mut);margin-top:10px}
.hint{margin:10px 0 4px}
.sl>div,.cl>div,.rl>div,.ar>div,.liru>div{position:absolute}
.sl>div,.cl>div{transform:translate(-50%,-50%);white-space:nowrap}
.pill{display:flex;gap:4px;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.45);background:rgba(110,120,130,.6)}
.pill.k0{background:rgba(67,160,71,.92)}.pill.k1{background:rgba(230,194,0,.95);color:#111}.pill.k2{background:rgba(240,138,36,.95);color:#111}.pill.k3{background:rgba(211,47,47,.95)}
.clb{display:flex;flex-direction:column;align-items:center;font-size:10px;line-height:1.15;padding:2px 6px;border-radius:8px;background:rgba(255,255,255,.9);color:#111;box-shadow:0 1px 3px rgba(0,0,0,.4)}
.clb b{font-size:11px}
@media (prefers-color-scheme:dark){.clb{background:rgba(18,22,28,.88);color:#fff}}
.rl>div{transform:translate(-50%,-115%);font-size:10px;color:var(--fg);opacity:.85;text-shadow:0 0 2px var(--mapbg)}
.ar>div{font-size:15px;line-height:15px;color:var(--fg)}
.lm{width:12px;height:12px;margin:-6px 0 0 -6px;border:2px solid var(--fg);border-radius:50%;background:var(--mapbg)}
.lm:after{content:"";position:absolute;left:3px;top:3px;width:6px;height:6px;border-radius:50%;background:var(--fg)}
.ll{left:9px;top:-8px;font-weight:700;font-size:11px;color:var(--fg);text-shadow:1px 0 var(--mapbg),-1px 0 var(--mapbg),0 1px var(--mapbg),0 -1px var(--mapbg)}
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
    for i in range(HORIZON_H):
        e.append(f'<g class="fr" data-h="{i}"' + ("" if i == 0 else ' style="display:none"') + ">")
        for r in RINGS_NM:
            p = by[(sec, r)]
            hh = p["hrs"][i] if len(p["hrs"]) > i else {"ph": None, "m": None, "k": None}
            x, y = to_nm(p["lat"], p["lon"])
            u, v = tp(x, y)
            code, cls = hh["ph"], pheno_cls(hh["ph"])
            e.append(f'<circle class="pt m{"n" if cls is None else cls}" cx="{u:.1f}" cy="{v:.1f}" r="6"/>')
            if code:
                e.append(f'<text class="t tb" text-anchor="middle" x="{u:.0f}" y="{v - 10:.0f}">{code}</text>')
            if code and hh["m"] is not None and hh["k"]:
                m = math.radians(hh["m"])
                length = min(hh["k"] / 2.0, 26.0)
                eu, ev = tp(x + length * math.sin(m), y + length * math.cos(m))
                while length > 6 and not (8 < eu < 192 and 8 < ev < 192):
                    length *= 0.9
                    eu, ev = tp(x + length * math.sin(m), y + length * math.cos(m))
                dx, dy = eu - u, ev - v
                n = math.hypot(dx, dy) or 1.0
                dx, dy = dx / n, dy / n
                bx, by_ = eu - 8 * dx, ev - 8 * dy
                e.append(f'<line class="arw" x1="{u:.1f}" y1="{v:.1f}" x2="{bx:.1f}" y2="{by_:.1f}"/>'
                         f'<polygon class="ah" points="{eu:.1f},{ev:.1f} {bx - 4 * dy:.1f},{by_ + 4 * dx:.1f} '
                         f'{bx + 4 * dy:.1f},{by_ - 4 * dx:.1f}"/>')
        e.append("</g>")
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


VIS = {"-RA": 8000, "-DZ": 6000, "RA": 6000, "DZ": 4000, "+RA": 3000, "-SHRA": 7000, "SHRA": 5000, "+SHRA": 3000,
       "-TSRA": 5000, "TSRA": 3000, "+TSRA": 1500, "TSGR": 1500, "-SN": 4000, "SN": 2000, "+SN": 800, "SHSN": 2000}


def _cloud(h, code):
    cl = h["cl"]
    if cl is None and not code:
        return ""
    cl = cl or 0
    amt = "FEW" if cl < 37 else "SCT" if cl < 62 else "BKN" if cl < 87 else "OVC"
    if code and amt in ("FEW", "SCT"):
        amt = "BKN"
    if cl < 12 and not code:
        return "NSC"
    base = 25 if h["T"] is None or h["Td"] is None else int(min(max(round((h["T"] - h["Td"]) * 4), 6), 120))
    return f"{amt}{base:03d}" + ("CB" if code and "TS" in code else "")


def _elems(h, code):
    ws, wg, wd = h["ws"], h["wg"], h["wd"]
    w = "VRB" if wd is None else f"{(int(round(wd / 10.0)) * 10) % 360 or 360:03d}"
    w += "//" if ws is None else f"{int(round(ws)):02d}"
    if ws is not None and wg is not None and wg - ws >= 10:
        w += f"G{int(round(wg)):02d}"
    return (w + "KT", f"{VIS.get(code, 9999):04d}", code or "", _cloud(h, code))


def _worst(near, far, i):
    c = [q["hrs"][i]["ph"] for q in (near, far) if len(q["hrs"]) > i and q["hrs"][i]["ph"]]
    if len(near["hrs"]) > i and near["hrs"][i].get("gw"):
        c.append(near["hrs"][i]["gw"])
    return max(c, key=lambda x: RANK[x]) if c else None


def _dur(hrs, i):
    n = 0
    while i + n < len(hrs) and hrs[i + n]["ph"]:
        n += 1
    return n, i + n >= len(hrs)


def metar_text(label, h, code, now):
    """METAR FITTIZIO dal modello (ora corrente)."""
    tt = lambda v: "//" if v is None else ("M" if round(v) < 0 else "") + f"{abs(round(v)):02d}"
    q = "" if h["P"] is None else f" Q{int(round(h['P']))}"
    return f"METAR {label} {now:%d%H%M}Z AUTO " + " ".join(x for x in _elems(h, code) if x) + f" {tt(h['T'])}/{tt(h['Td'])}{q}"


def sector_metar(sec, by, now):
    near, far = by[(sec, RINGS_NM[0])], by[(sec, RINGS_NM[1])]
    return metar_text(f"LIRU-{sec}", near["hrs"][0], _worst(near, far, 0), now) if near["hrs"] else ""


def taf_text(sec, by, now):
    return _taf(f"LIRU-{sec}", by[(sec, RINGS_NM[0])], by[(sec, RINGS_NM[1])], now)


def _taf(label, near, far, now):
    """TAF FITTIZIO: gruppo base + TEMPO (fenomeni brevi) e BECMG (cambi che restano)."""
    hrs = near["hrs"]
    if not hrs or not hrs[0]["t"]:
        return "Dati insufficienti per il TAF."
    T = lambda h: dt.datetime.strptime(h["t"], "%Y-%m-%dT%H:%M")
    z = lambda i: T(hrs[i]) - dt.timedelta(hours=1)               # inizio dell'ora i
    segs = []
    for i, h in enumerate(hrs):
        e = _elems(h, _worst(near, far, i))
        if segs and segs[-1][2] == e:
            segs[-1][1] = i
        else:
            segs.append([i, i, e])
    lines = [f"TAF {label} {now:%d%H%M}Z {z(0):%d%H}/{T(hrs[-1]):%d%H} " + " ".join(x for x in segs[0][2] if x)]
    prev = segs[0][2]
    for k, (a, b, e) in enumerate(segs[1:], 1):
        if e == prev:
            continue
        last = k == len(segs) - 1
        sig = bool(e[2]) and RANK[e[2]] >= 6
        if b - a + 1 <= 2 and (not last or sig):
            diff = [x for x, p in zip(e, prev) if x != p and x]
            if diff:
                lines.append(f"TEMPO {z(a):%d%H}/{T(hrs[b]):%d%H} " + " ".join(diff))
        else:
            parts = [x or ("NSW" if j == 2 else "") for j, (x, p) in enumerate(zip(e, prev)) if x != p]
            lines.append(f"BECMG {z(a):%d%H}/{(z(a) + dt.timedelta(hours=1)):%d%H} " + " ".join(x for x in parts if x))
            prev = e
    return "\n".join(lines)


def sector_card(sec, by, now):
    pts = [by[(sec, r)] for r in RINGS_NM]
    codes = [_worst(pts[0], pts[1], i) for i in range(len(pts[0]["hrs"]))]
    wc = max((c for c in codes if c), key=lambda c: RANK[c], default=None)
    if wc:
        wt = pts[0]["hrs"][codes.index(wc)]["t"][11:13]
        head = (f'<div class="hd"><b class="sn">{sec}</b><span class="b c{pheno_cls(wc)}"><b>{wc}</b></span>'
                f'<span class="s">nel settore, entro le {wt}Z</span></div>')
    else:
        head = f'<div class="hd"><b class="sn">{sec}</b><span class="s">nessun fenomeno nel modello</span></div>'
    rings = ""
    for p in pts:
        if p["ph"]:
            line = f'<span class="b c{pheno_cls(p["ph"])}"><b>{p["ph"]}</b></span> entro le {p["ph_time"]}'
            if p["motion"]:
                line += f' · verso {p["motion"][0]} {p["motion"][1]:.0f} kt'
            n_, op_ = _dur(p["hrs"], p["ph_i"])
            line += f' · durata {"≥" if op_ else "~"}{n_} h'
            if p["eta"] is not None:
                line += f' · su LIRU {arrival_text(p, now)}'
        else:
            line = '<span class="s">nessun fenomeno</span>'
        rings += (f'<div class="ring"><b>{p["ring"]} NM</b> {line}<br>'
                  f'{badge("Conv", p["conv"])}{badge("Vento", p["windlvl"])}'
                  f'<div class="s">CAPE {fmt(p["cape"])} · raff {fmt(p["gust"])} kt · pioggia {fmt(p["precip"], 1)} mm/h</div></div>')
    hours = by[(sec, RINGS_NM[0])]["hrs"]
    btns = "".join(f'<button class="hb{" on" if i == 0 else ""}" data-h="{i}">{(h["t"] or "?")[11:13]}Z</button>'
                   for i, h in enumerate(hours))
    return (f'<div class="card sp" id="sp-{sec}" hidden>{head}<pre class="taf">{html.escape(sector_metar(sec, by, now) + chr(10) + chr(10) + taf_text(sec, by, now))}</pre>'
            f'<div class="s">METAR e TAF FITTIZI generati dal modello: non sono ufficiali, non usarlo per la navigazione. '
            f'Immagine: situazione prevista nell\'ora che termina all\'orario scelto.</div>'
            f'<div class="ctl">{btns}</div><div class="sc">{sector_map(sec, by)}<div class="info">{rings}</div></div></div>')


MAP_HTML = """<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<div class="card mapcard"><div id="map"></div>
<div class="bar"><button id="pl" class="btn">⏸</button><input type="range" id="sl" min="0" max="0" value="0"><b id="tl">…</b></div>
<div class="bar opts"><label class="opt"><input type="checkbox" id="cr" checked> Radar</label>
<label class="opt"><input type="checkbox" id="ca" checked> Spazi aerei</label>
<select id="cs"><option value="">Nuvole · no</option><option value="ir">Nuvole · infrarosso</option><option value="geocolour">Nuvole · colori</option></select>
<select id="ap"><option value="">Vista: LIRU 50 NM</option><option value="IT">Italia intera</option>__APOPT__</select>
<select id="cv"><option value="1">Velocità: lenta</option><option value="2">media</option><option value="3">veloce</option></select></div>
<div class="s" id="lg"></div><div class="s" id="st"></div><div id="apinfo" style="display:none"></div>
<div class="s">Radar: <a href="https://www.rainviewer.com/">Weather data by RainViewer</a> (ultima ora). PREVISIONE = figure ricavate dalla griglia del modello ICON-EU (punti ogni 11 km): forma, dimensione, moto e durata sono stime, non un radar previsto.
Nuvole: Contains modified EUMETSAT Meteosat data. Sfondo: Natural Earth. Spazi aerei (se attivi): OpenAIP, uso non commerciale, solo indicativi, non per la navigazione. Tocca un settore per METAR e TAF.</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>(function(){var D=__DATA__,C=D.c,$=function(i){return document.getElementById(i)},
dark=matchMedia("(prefers-color-scheme:dark)").matches,
K=dark?{land:"#1b2230",ln:"#9db1c6",ring:"#c3d0dd",lake:"#12324d",sea:"#0b1b2b",ar:"#f5f7fa"}:{land:"#f3f0e7",ln:"#5d6c7c",ring:"#40566b",lake:"#8fbfe4",sea:"#bcd8ee",ar:"#16202b"},
SC=["#43a047","#e6c200","#f08a24","#d32f2f"],
ll=function(l){return l.map(function(q){return [q[1],q[0]]})},
esc=function(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;")},
IT=[[35.3,6.5],[47.2,19]],FT=(D.cl&&D.cl.t&&D.cl.t.length)?D.cl.t:D.ft;
var map=L.map("map",{zoomSnap:.25,attributionControl:false});
map.fitBounds(L.latLng(C).toBounds(190000));$("map").style.background=K.land;
["land","base","sat","rad"].forEach(function(n,i){map.createPane(n).style.zIndex=[205,210,250,260][i]});
var LC=D.geo.coast.map(function(l){return L.polyline(ll(l),{color:K.ln,weight:1.3,opacity:.9,interactive:false}).addTo(map)});
D.geo.lakes.forEach(function(r){L.polygon(ll(r),{pane:"base",stroke:false,fillColor:K.lake,fillOpacity:1,interactive:false}).addTo(map)});
fetch("italia2.json").then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(j){var d=j.data;
L.polygon([[[35.3,6],[47.3,6],[47.3,19],[35.3,19]]].concat(d.land.map(ll)),{pane:"land",stroke:false,fillColor:K.sea,fillOpacity:1,interactive:false}).addTo(map);
d.lakes.forEach(function(r){L.polygon(ll(r),{pane:"base",stroke:false,fillColor:K.lake,fillOpacity:1,interactive:false}).addTo(map)});
d.coast.forEach(function(l){L.polyline(ll(l),{color:K.ln,weight:1.2,opacity:.9,interactive:false}).addTo(map)});
d.borders.forEach(function(l){L.polyline(ll(l),{color:K.ln,weight:1,opacity:.5,dashArray:"4 3",interactive:false}).addTo(map)});
LC.forEach(function(l){map.removeLayer(l)})}).catch(function(){});
[25,50].forEach(function(r){L.circle(C,{radius:r*1852,color:K.ring,weight:1,opacity:.55,dashArray:"2 6",fill:false,interactive:false}).addTo(map);
L.marker([C[0]+r/60,C[1]],{interactive:false,icon:L.divIcon({className:"rl",iconSize:[0,0],html:"<div>"+r+" NM</div>"})}).addTo(map)});
D.a.forEach(function(a){L.circleMarker([a[1],a[2]],{radius:3.5,color:K.ar,weight:1,fillColor:K.land,fillOpacity:1,interactive:false}).addTo(map)
.bindTooltip(a[0],{permanent:true,direction:"bottom",offset:[0,4],className:a[3]?"loc":""})});
L.marker(C,{interactive:false,icon:L.divIcon({className:"liru",iconSize:[0,0],html:'<div class="lm"></div><div class="ll">LIRU</div>'})}).addTo(map);
var SW={},SL={};
function openSec(k){var id="sp-"+k;document.querySelectorAll(".sp").forEach(function(p){p.hidden=p.id!=id});
var e=document.getElementById(id);if(e)e.scrollIntoView({behavior:"smooth",block:"start"});Object.keys(SW).forEach(function(x){SW[x].setStyle({weight:x==k?3:1})})}
Object.keys(D.sec).forEach(function(k){var s=D.sec[k];SW[k]=L.polygon(s.poly,{color:K.ring,weight:1,opacity:.45,fillColor:"#888",fillOpacity:0}).addTo(map);SW[k].on("click",function(){openSec(k)});
SL[k]=L.marker(s.lab,{interactive:false,icon:L.divIcon({className:"sl",iconSize:[0,0],html:""})}).addTo(map)});
function secs(F,h){Object.keys(D.sec).forEach(function(k){var s=D.sec[k],j=F?s.t.indexOf(FT[h]):0;if(j<0)j=Math.min(h,s.t.length-1);
var c=s.k[j],t=s.c[j];SW[k].setStyle({fillColor:c==null?"#888":SC[c],fillOpacity:c==null?0:.14});
SL[k].setIcon(L.divIcon({className:"sl",iconSize:[0,0],html:'<div class="pill k'+(c==null?"n":c)+'"><b>'+k+'</b>'+(t?'<span>'+t+'</span>':'')+'</div>'}))})}
var AL=L.layerGroup().addTo(map);$("ca").parentNode.style.display="none";
fetch("airspace.json").then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(j){var AC={4:"#d6336c",13:"#d6336c",7:"#3b82f6",26:"#3b82f6",1:"#ef4444",2:"#ef4444",3:"#ef4444"};
(j.data||[]).forEach(function(a){a.r.forEach(function(r){L.polygon(ll(r),{color:AC[a.t]||"#8b8f96",weight:1.2,dashArray:"5 3",fill:false}).bindTooltip(a.n,{sticky:true}).addTo(AL)})});
if((j.data||[]).length)$("ca").parentNode.style.display=""}).catch(function(){});
$("ca").onchange=function(){this.checked?AL.addTo(map):map.removeLayer(AL)};
map.on("zoomend",function(){map.getContainer().classList.toggle("far",map.getZoom()<7.5)});
var APR=L.layerGroup().addTo(map);
$("ap").onchange=function(){var v=this.value,b=$("apinfo");b.style.display="none";APR.clearLayers();
if(v==="")map.fitBounds(L.latLng(C).toBounds(190000));else if(v==="IT")map.fitBounds(IT);
else{var a=D.ap[v]||{};[25,50].forEach(function(r){L.circle([a.lat,a.lon],{radius:r*1852,color:"#4da3ff",weight:1.6,dashArray:"5 5",fill:false,interactive:false}).addTo(APR);
L.marker([a.lat+r/60,a.lon],{interactive:false,icon:L.divIcon({className:"rl",iconSize:[0,0],html:"<div>"+r+" NM</div>"})}).addTo(APR)});
L.circleMarker([a.lat,a.lon],{radius:6,color:"#4da3ff",weight:2,fillColor:"#fff",fillOpacity:1,interactive:false}).addTo(APR);
map.fitBounds(L.latLng([a.lat,a.lon]).toBounds(190000));
b.innerHTML="<b>"+v+"</b> (METAR e TAF ufficiali)"+'<pre class="taf">'+esc(a.metar||"nessun METAR ufficiale ricevuto")+String.fromCharCode(10,10)+esc(a.taf||"nessun TAF ufficiale ricevuto")+"</pre>";b.style.display=""}};
var LAY={geocolour:"mtg_fd:rgb_geocolour",ir:"mtg_fd:ir105_hrfi"},fr=[],rad=[],sat=[],ix=0,tm=null,run=true,nr=0,FG=[];
$("lg").innerHTML=["pioggia debole","pioggia/neve","rovesci","temporale"].map(function(t,i){return '<span class="lgd" style="background:'+SC[i]+'">'+t+'</span>'}).join(" ");
function arrow(g,lat,lon,m,v){var d=Math.min(v/2,26)*1852,a=m*Math.PI/180,la=lat+d*Math.cos(a)/111320,lo=lon+d*Math.sin(a)/(111320*Math.cos(lat*Math.PI/180));
g.push(L.polyline([[lat,lon],[la,lo]],{color:K.ar,weight:2.2,interactive:false}));
g.push(L.marker([la,lo],{interactive:false,icon:L.divIcon({className:"ar",iconSize:[0,0],html:'<div style="transform:translate(-50%,-50%) rotate('+m+'deg)">▲</div>'})}))}
function mkC(h){var g=[];(D.cl.c[h]||[]).forEach(function(c){var col=SC[c.k];
g.push(L.polygon(c.p,{color:col,weight:2,opacity:.95,fillColor:col,fillOpacity:.3,interactive:false}));
if(c.m!=null&&c.v)arrow(g,c.c[0],c.c[1],c.m,c.v);
if(c.lb){var sz=(c.L-c.W<5)?"ø~"+c.L+" NM":"~"+c.L+"×"+c.W+" NM";
g.push(L.marker(c.c,{interactive:false,icon:L.divIcon({className:"cl",iconSize:[0,0],html:'<div class="clb"><b>'+c.x+'</b><span>'+(c.v!=null?c.v+" kt · ":"")+(c.o?"≥":"~")+c.d+" h</span><span>"+sz+"</span></div>"})}))}});
return L.layerGroup(g)}
function mkM(h){var g=[];D.p.forEach(function(p){var i=p.t.indexOf(FT[h]);if(i<0)return;var e=p.h[i];if(!e||!e[0])return;
g.push(L.circleMarker([p.lat,p.lon],{radius:6,color:"#000",weight:1.5,fillColor:SC[e[3]],fillOpacity:1,interactive:false}));
if(e[1]!=null&&e[2])arrow(g,p.lat,p.lon,e[1],e[2])});return L.layerGroup(g)}
function addF(){if(!FT||!FT.length){$("st").textContent+=" · previsione non disponibile";return}
FT.forEach(function(t,h){fr.push(Date.parse(t+":00Z")/1000);FG.push(D.cl&&D.cl.c[h]?mkC(h):mkM(h))});
$("st").textContent+=" · Previsione: "+FT.length+" ore"+(D.cl?" (griglia)":" (solo punti)")}
function mkSat(){sat.forEach(function(l){map.removeLayer(l)});sat=[];var k=$("cs").value;if(!k)return;
(D.s[k]||[]).forEach(function(t){sat.push(L.tileLayer.wms("https://view.eumetsat.int/geoserver/wms",{layers:LAY[k],format:"image/png",version:"1.3.0",time:t,pane:"sat",opacity:0}).addTo(map))});show(ix)}
function show(i){ix=i;if(!fr.length)return;var T=fr[i],F=i>=nr,h=i-nr;$("sl").value=i;
$("tl").textContent=(F?"PREVISIONE · entro le ":"")+new Date(T*1000).toISOString().substr(11,5)+"Z";$("tl").className=F?"fc":"";
rad.forEach(function(l,k){l.setOpacity(!F&&k==i&&$("cr").checked?.7:0)});
FG.forEach(function(g,k){if(F&&k==h)g.addTo(map);else map.removeLayer(g)});
var k=$("cs").value,ts=D.s[k]||[],b=-1;ts.forEach(function(t,j){if(Date.parse(t)/1000<=T+300)b=j});if(b<0)b=0;
sat.forEach(function(l,j){l.setOpacity(!F&&j==b?.8:0)});secs(F,h)}
function step(){if(!run)return;show((ix+1)%fr.length);tm=setTimeout(step,ix==fr.length-1?2500:({1:1500,2:900,3:450})[$("cv").value])}
function go(){nr=fr.length;addF();$("sl").max=Math.max(fr.length-1,0);ix=Math.max(nr-1,0);mkSat();show(ix);tm=setTimeout(step,900)}
var te=0;
fetch("https://api.rainviewer.com/public/weather-maps.json").then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}).then(function(j){
var p=(j.radar&&j.radar.past)||[];if(!p.length)throw new Error("nessuna immagine");
p.slice(-7).forEach(function(f){fr.push(f.time);var l=L.tileLayer(j.host+f.path+"/256/{z}/{x}/{y}/2/1_1.png",{pane:"rad",opacity:0,maxNativeZoom:7,maxZoom:12}).addTo(map);
l.on("tileerror",function(){te++;$("st").textContent="Radar: "+nr+" immagini, "+te+" tile non caricati"});rad.push(l)});
$("st").textContent="Radar: "+fr.length+" immagini"})
.catch(function(e){rad.forEach(function(l){map.removeLayer(l)});rad=[];var q=D.s.ir&&D.s.ir.length?D.s.ir:(D.s.geocolour||[]);
fr=q.map(function(t){return Date.parse(t)/1000});$("st").textContent="Radar non disponibile ("+(e&&e.message||e)+")"})
.then(function(){try{go()}catch(e){$("st").textContent+=" · errore: "+e.message}});
$("pl").onclick=function(){run=!run;this.textContent=run?"⏸":"▶";clearTimeout(tm);if(run)step()};
$("sl").oninput=function(){run=false;$("pl").textContent="▶";clearTimeout(tm);show(+this.value)};
$("cr").onchange=function(){show(ix)};$("cs").onchange=mkSat})();</script>"""


def decode_metar(raw):
    """Chips colorati con i gruppi principali di un METAR."""
    ch = []
    add = lambda t, l: ch.append(f'<span class="b c{l}">{html.escape(t)}</span>')
    m = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?KT\b", raw)
    if m:
        ws, gs = int(m.group(2)), int(m.group(3) or 0)
        add(f"Vento {'var.' if m.group(1) == 'VRB' else m.group(1) + '°'} {ws} kt" + (f" raff. {gs}" if gs else ""), wind_level(max(ws, gs)))
    if "CAVOK" in raw:
        add("CAVOK", 0)
    else:
        v = re.search(r"\s(\d{4})(?=\s)", raw)
        if v:
            n_ = int(v.group(1))
            add("Visibilità ≥10 km" if n_ >= 9999 else f"Visibilità {n_} m", 3 if n_ < 1500 else 2 if n_ < 5000 else 1 if n_ < 8000 else 0)
    for w in re.findall(r"(?<=\s)([-+]?(?:VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ)+)(?=\s)", raw):
        add(w, 3 if "TS" in w else 2 if any(x in w for x in ("SH", "FG", "FZ", "GR")) else 1)
    for amt, base, cb in re.findall(r"\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?", raw):
        b = int(base)
        lvl = 3 if cb == "CB" else 2 if cb == "TCU" else ((3 if b < 5 else 2 if b < 10 else 1 if b < 20 else 0) if amt in ("BKN", "OVC") else 0)
        add(f"{amt} {b * 100} ft" + (f" {cb}" if cb else ""), lvl)
    t = re.search(r"\s(M?\d{2})/(M?\d{2})(?=\s)", raw)
    if t:
        add(f"T {t.group(1).replace('M', '-')}° · Td {t.group(2).replace('M', '-')}°", "n")
    q = re.search(r"\bQ(\d{4})\b", raw)
    if q:
        add(f"QNH {q.group(1)}", "n")
    return "".join(ch)


def fmt_taf(raw):
    return re.sub(r"\s+(?=(?:TEMPO|BECMG|FM\d{6}|PROB\d{2})\b)", "\n   ", raw)


def cloud_types(h, code, cape):
    """Tipologia di nubi stimata (euristica) da fenomeno, CAPE e copertura bassa/media/alta."""
    lo, mi, hi = (h.get("cl") or 0), (h.get("cm") or 0), (h.get("ch") or 0)
    t = []
    if code and "TS" in code:
        t.append("CB")
    elif code and "SH" in code:
        t.append("TCU" if (cape or 0) >= 800 else "CU")
    elif code and any(x in code for x in ("RA", "DZ", "SN")):
        t.append("NS")
    elif lo >= 40:
        t.append("SC" if (cape or 0) < 400 else "CU")
    if mi >= 40:
        t.append("AS" if (code and mi >= 80) else "AC")
    if hi >= 40:
        t.append("CI")
    return t or ["poche"]


def lvl3(v, a, b, c):
    return None if v is None else 0 if v < a else 1 if v < b else 2 if v < c else 3


def tile(label, val, unit, lvl):
    return f'<div class="tile c{"n" if lvl is None else lvl}"><span class="tl">{label}</span><b>{val}</b> <span class="tu">{unit}</span></div>'


def station_card(icao, metar, m_off, taf, t_off):
    tag = lambda off: '<span class="b c0">UFFICIALE</span>' if off else '<span class="b c1">FITTIZIO · dal modello</span>'
    mh = (f'<div class="chips">{decode_metar(metar)}</div><pre class="taf">{html.escape(metar)}</pre>'
          if metar else '<div class="s">METAR non ricevuto</div>')
    th = f'<pre class="taf">{html.escape(fmt_taf(taf))}</pre>' if taf else '<div class="s">TAF non ricevuto</div>'
    return (f'<div class="card st"><div class="hd"><b class="sn">{icao}</b></div>'
            f'<div class="lab">METAR {tag(m_off)}</div>{mh}<div class="lab">TAF {tag(t_off)}</div>{th}</div>')


def sector_data(points):
    by = {(p["sector"], p["ring"]): p for p in points[1:]}
    out = {}
    for sec, brg in SECTORS.items():
        near, far = by[(sec, RINGS_NM[0])], by[(sec, RINGS_NM[1])]
        codes = [_worst(near, far, i) for i in range(len(near["hrs"]))]
        out[sec] = {"t": [h["t"] for h in near["hrs"]], "c": codes, "k": [pheno_cls(c) for c in codes],
                    "poly": [list(CENTER)] + [list(destination(CENTER[0], CENTER[1], brg - 22.5 + 4.5 * i, 50)) for i in range(11)],
                    "lab": list(destination(CENTER[0], CENTER[1], brg, 33))}
    return out


def points_data(points):
    return [{"lat": p["lat"], "lon": p["lon"], "t": [h["t"] for h in p["hrs"]],
             "h": [([h["ph"], h["m"], h["k"], pheno_cls(h["ph"]), *_dur(p["hrs"], i)] if h["ph"] else [None, None, None, None, 0, 0])
                   for i, h in enumerate(p["hrs"])]} for p in points]


def map_data(points, sat, grid=None, apdata=None):
    pts = [{"lat": p["lat"], "lon": p["lon"], "ph": p["ph"], "c": pheno_cls(p["ph"]), "t": p["ph_time"],
            "m": p["mdeg"], "k": p["mkt"]} for p in points if p["ph"]]
    lines = [[list(CENTER), list(destination(CENTER[0], CENTER[1], b + 22.5, 50))] for b in SECTORS.values()]
    return {"c": list(CENTER), "p": points_data(points), "ft": [h["t"] for h in points[1]["hrs"]] if len(points) > 1 else [], "l": lines, "s": sat or {}, "a": [[k, v[0], v[1], 0 if k in AIRPORT_MENU else 1] for k, v in {**AIRPORTS, **AIRPORT_MENU}.items()],
            "ap": apdata or {},
            "cl": ({"t": grid["t"], "c": grid["cl"]} if grid else None), "geo": GEO, "sec": sector_data(points)}


def compass_box(sec, by, now):
    pts = [by[(sec, r)] for r in RINGS_NM]
    w = max(pts, key=lambda p: RANK.get(p["ph"], 0))
    lv = lambda key: max((p[key] for p in pts if p[key] is not None), default=None)
    chip = lambda lab, v: f'<span class="chip k{"n" if v is None else v}">{lab}</span>'
    chips = chip("C", lv("conv")) + chip("V", lv("windlvl"))
    if not w["ph"]:
        return f'<div class="cb cn" data-s="{sec}"><b>{sec}</b><br><span class="s">nessun fenomeno</span><br>{chips}</div>'
    t = f'<b>{sec}</b><br><span class="cd">{w["ph"]}</span><br>{w["ph_time"]} · {w["ring"]} NM'
    if w["motion"]:
        t += f'<br>→ {w["motion"][0]} {w["motion"][1]:.0f} kt'
    if w["eta"] is not None:
        t += f'<br>LIRU {arrival_text(w, now).split(" ")[0]}'
    return f'<div class="cb c{pheno_cls(w["ph"])}" data-s="{sec}">{t}<br>{chips}</div>'


def compass_html(by, now):
    return '<div id="map"></div>'


PANEL_JS = """<script>(function(){var P=document.querySelectorAll(".sp");
document.querySelectorAll(".cb").forEach(function(b){b.onclick=function(){var id="sp-"+b.dataset.s;
P.forEach(function(p){p.hidden=p.id!=id});document.getElementById(id).scrollIntoView({behavior:"smooth",block:"start"})}});
document.querySelectorAll(".hb").forEach(function(b){b.onclick=function(){var s=b.closest(".sp");
s.querySelectorAll(".hb").forEach(function(x){x.classList.toggle("on",x==b)});
s.querySelectorAll(".fr").forEach(function(g){g.style.display=g.dataset.h==b.dataset.h?"":"none"})}})})();</script>"""


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


def render(points, metars, now, missing=(), sat=None, grid=None, apdata=None):
    center = points[0]
    by = {(p["sector"], p["ring"]): p for p in points[1:]}
    ranked = sorted((p for p in points[1:] if p["conv"] is not None), key=lambda p: (p["conv"], p["cape"] or 0), reverse=True)
    top = ""
    if ranked and ranked[0]["conv"] >= 1:
        t = ranked[0]
        top = f'Ambiente più favorevole alla convezione: <b>{t["name"]} NM</b> ({LABELS[t["conv"]]}, CAPE {fmt(t["cape"])} J/kg).'
    elif ranked:
        top = "Nessun settore con ambiente convettivo significativo nelle prossime ore."
    liru_ph = f'<b>{center["ph"]}</b> entro le {center["ph_time"]}' if center["ph"] else "nessun fenomeno"
    h0 = center["hrs"][0] if center["hrs"] else None
    ft = None if center["frz"] is None else round(center["frz"] * 3.281, -2)
    ctypes = cloud_types(h0, center["ph"], center["cape"]) if h0 else ["–"]
    tiles = "".join([
        tile("CAPE", fmt(center["cape"]), "J/kg", lvl3(center["cape"], 300, 1000, 2000)),
        tile("Vento", fmt(center["wind"]), "kt", lvl3(center["wind"], 15, 25, 35)),
        tile("Raffiche", fmt(center["gust"]), "kt", center["windlvl"]),
        tile("Zero termico", fmt(ft), "ft", None if ft is None else 3 if ft <= 3000 else 2 if ft <= 6000 else 1 if ft <= 9000 else 0),
        tile("Nubi (stima)", " ".join(ctypes), "", max(({"CB": 3, "TCU": 2, "CU": 1, "NS": 1}.get(c, 0) for c in ctypes), default=0))])
    order = sorted(SECTORS, key=lambda sc: (-max(RANK.get(_worst(by[(sc, 25)], by[(sc, 50)], i), 0) for i in range(len(by[(sc, 25)]["hrs"]))), list(SECTORS).index(sc)))
    cards = "".join(sector_card(sc, by, now) for sc in order)
    age_js = ('<div id="age" class="mut"></div><script>(function(){var t=EPOCH*1000;function u(){'
              'var m=Math.round((Date.now()-t)/60000),e=document.getElementById("age");'
              'if(m>15){e.className="stale";e.textContent="Dati vecchi di "+m+" min: non fidarti";}'
              'else{e.className="mut";e.textContent="Dati di "+m+" min fa";}}'
              'u();setInterval(u,20000);})();</script>').replace("EPOCH", str(int(now.timestamp())))
    map_card = MAP_HTML.replace("__DATA__", json.dumps(map_data(points, sat, grid, apdata))).replace("__APOPT__", AP_OPTS)
    mm = {k: v for k, v in metars if v.startswith(("METAR", "SPECI"))}
    ap = apdata or {}
    liru_m = mm.get("LIRU") or (metar_text("LIRU", h0, center["ph"], now) if h0 else "")
    stations = (station_card("LIRU", liru_m, "LIRU" in mm, _taf("LIRU", center, center, now) if h0 else "", False)
                + station_card("LIRA", mm.get("LIRA", ""), True, ap.get("LIRA", {}).get("taf", ""), True)
                + station_card("LIRF", mm.get("LIRF", ""), True, ap.get("LIRF", {}).get("taf", ""), True))
    legend = ("Severità crescente: -RA, -DZ, RA, DZ, +RA, -SHRA, SHRA, +SHRA, -TSRA, TSRA, +TSRA, TSGR (neve: -SN, SN, +SN, SHSN). "
              "Freccia = direzione di spostamento (vento a 700 hPa). «Su LIRU» = ora stimata di arrivo in linea retta (±30'). "
              "Le tipologie di nubi sono stimate dal modello. Quando un METAR o un TAF è «fittizio» è generato dal modello: non è ufficiale.")
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Cruscotto LIRU</title><style>{CSS}</style></head><body>
<header class="top"><div><h1>Cruscotto LIRU</h1><div class="mut">50 NM · {MODEL} · aggiornato {now:%H:%M}Z · v1.3</div></div>{age_js}</header>
<div class="card warn mut">Supporto informativo, non sostituisce briefing, METAR/TAF/SIGMET, ATC e osservazione diretta delle nubi.</div>
{map_card}
<div class="card"><div class="lab" style="margin:0 0 6px">LIRU · prossime ore</div><div class="tiles">{tiles}</div>
<div class="s">{liru_ph} · Regime: {html.escape(regime(points))}<br>{top}</div>{missing_note(missing)}</div>
<div class="mut hint">Tocca un settore sulla mappa per METAR/TAF fittizi e immagine prevista.</div>
{cards}
<h2>METAR e TAF</h2>{stations}
<div class="mut">{legend}</div>
{PANEL_JS}</body></html>"""


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(dt.timezone.utc)
    points = build_points()
    series = cached("model", 25, lambda: fetch_all(points), now)
    if len(series) != len(points):
        raise RuntimeError(f"Attesi {len(points)} punti, ricevuti {len(series)}")
    trim_past(series, now)
    analyse(points, series)
    missing = missing_vars(series)
    metars = fetch_metar()
    sat = cached("sat", 9, sat_times, now)
    grid = fetch_grid(now)
    if os.environ.get("OPENAIP_API_KEY"):
        try:
            cached("airspace", 1440, fetch_airspace, now)
        except Exception as e:  # noqa: BLE001
            print("spazi aerei non disponibili:", e)
    try:
        cached("italia2", 525600 * 10, fetch_italy, now)       # una volta sola
    except Exception as e:  # noqa: BLE001
        print("confini d'Italia non disponibili:", e)
    apdata = fetch_airports()
    ga = grid_analysis(grid, now)
    apply_grid(points, ga)

    DOCS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(points, metars, now, missing, sat, ga, apdata), encoding="utf-8")

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