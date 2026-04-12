#!/usr/bin/env python3

import sys
import json
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import typer
from typing import List, Optional, Dict, Any

app = typer.Typer(add_completion=False)

# Constants
URL = "https://meteo.arso.gov.si/uploads/probase/www/observ/surface/text/sl/observationAms_KOPER_KAPET-IJA_latest.xml"
TREND_URL = "http://hmljn.arso.gov.si/vode/podatki/amp/H9350_t_30.html"
WARNING_URL = "https://meteo.arso.gov.si/uploads/probase/www/warning/text/sl/warning_SLOVENIA_SOUTH-WEST_latest_CAP.xml"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DIR_TO_DEG = {"N": 0, "SV": 45, "V": 90, "JV": 135, "J": 180, "JZ": 225, "Z": 270, "SZ": 315}

# -----------------------------
# Utils
# -----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def err(msg: str, code="GENERIC", details=None, as_json=False):
    payload = {"ok": False, "error": msg, "code": code, "details": details or {}}
    if as_json:
        print(json.dumps(payload))
    else:
        typer.secho(f"ERR[{code}] {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)

def ok(data: Dict[str, Any], as_json=False, pretty=False):
    if as_json:
        print(json.dumps({"ok": True, "data": data}, indent=2 if pretty else None, sort_keys=True))
    else:
        typer.echo(data["summary"])
    raise typer.Exit(0)

# -----------------------------
# Data Fetching
# -----------------------------

def fetch_forecast(timeout: float) -> Dict[str, Any]:
    try:
        params = {"latitude": 45.5369, "longitude": 13.6619, "hourly": "precipitation", "forecast_days": 1}
        r = requests.get(FORECAST_URL, params=params, timeout=timeout)
        r.raise_for_status()
        rain = r.json().get("hourly", {}).get("precipitation", [])
        total_next_3h = sum(rain[:3]) if rain else 0
        return {"rain_expected": total_next_3h}
    except Exception:
        return {"rain_expected": 0}

def fetch_current(timeout: float) -> Dict[str, Any]:
    r = requests.get(URL, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    md = root.find(".//metData")
    
    def getf(tag):
        el = md.find(tag)
        return float(el.text) if el is not None and el.text else None

    return {
        "time": md.findtext("valid"),
        "air_temp": getf("t"),
        "water_temp": getf("tw"),
        "wind": getf("ffavg_val_kmh"),
        "gusts": getf("ffmax_val_kmh"),
        "sun": getf("gSunRadavg"),
        "wind_dir": md.findtext("dd_shortText"),
    }

def fetch_trend(limit: int, timeout: float) -> List[Dict]:
    r = requests.get(TREND_URL, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table.podatki tbody tr")[:limit]
    
    data = []
    for row in rows:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) < 3: continue
        try:
            data.append({
                "time": cols[0],
                "temp": float(cols[1]) if cols[1] != "-" else None,
                "level": float(cols[2]) if cols[2] != "-" else None
            })
        except ValueError: continue
    return data

def fetch_warning(timeout: float) -> Optional[Dict]:
    try:
        r = requests.get(WARNING_URL, timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        # Using {*} to ignore namespaces in CAP XML
        info = root.find(".//{*}info")
        if info is None: return None

        wtype = None
        for p in info.findall(".//{*}parameter"):
            if p.findtext(".//{*}valueName") == "awareness_type":
                val = p.findtext(".//{*}value")
                if val and ";" in val: wtype = val.split(";")[1].strip()

        return {
            "event": info.findtext(".//{*}event"),
            "severity": info.findtext(".//{*}severity"),
            "type": wtype,
            "headline": info.findtext(".//{*}headline"),
        }
    except Exception:
        return None

# -----------------------------
# Logic & Scoring
# -----------------------------

def classify_wind(dir_short: Optional[str]) -> Dict[str, str]:
    if not dir_short: return {"name": "calm", "type": "none", "risk": "low"}
    deg = DIR_TO_DEG.get(dir_short.upper())
    if deg is None: return {"name": "unknown", "type": "unknown", "risk": "unknown"}

    if 30 <= deg <= 80: return {"name": "Bura", "type": "cold_gusty", "risk": "high"}
    if 110 <= deg <= 170: return {"name": "Jugo", "type": "warm_wet", "risk": "medium"}
    if 280 <= deg <= 330: return {"name": "Maestral", "type": "thermal", "risk": "low"}
    if deg >= 330 or deg <= 20: 
        name = "Burin" if 350 <= deg or deg <= 10 else "Tramontana"
        return {"name": name, "type": "cold_dry", "risk": "medium"}
    if 200 <= deg <= 250: return {"name": "Lebić", "type": "gusty", "risk": "medium"}
    if 80 <= deg <= 110: return {"name": "Levant", "type": "cold_east", "risk": "medium"}
    if 250 <= deg <= 280: return {"name": "Pulenat", "type": "storm_west", "risk": "high"}
    
    return {"name": "local", "type": "unknown", "risk": "low"}

def trend_direction(values: List[float]) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2: return "unknown"
    delta = vals[0] - vals[-1]
    return "rising" if delta > 0.5 else "falling" if delta < -0.5 else "stable"

def detect_peak(values: List[float]) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 3: return "none"
    if vals[0] < vals[1] > vals[2]: return "recent_peak"
    if vals[0] > vals[1] < vals[2]: return "recent_low"
    return "none"

def bathing_score(d: Dict) -> int:
    wt = d.get("water_temp")
    if wt is None: return -999
    
    score = 0
    w, gusts, t = d.get("wind") or 0, d.get("gusts") or 0, d.get("air_temp") or 0

    # Water Temp
    if wt < 11: score -= 6
    elif wt < 13: score -= 2
    elif wt < 16: score += 1
    elif wt < 19: score += 2
    elif wt <= 22: score += 3
    elif wt < 25: score += 1
    else: score -= 2

    # Environmental
    if w < 5: score += 2
    elif w < 10: score += 1
    elif w >= 20: score -= 3
    if gusts > 25: score -= 2
    if t >= 20: score += 2
    elif t >= 15: score += 1
    else: score -= 2
    if (d.get("sun") or 0) > 300: score += 1
    if d.get("wind_dir") in ("V", "SV"): score -= 2
    if d.get("warning_type") == "wind": score -= 2

    # Contextual
    if wt >= 18 and w >= 12: score += 2
    if wt < 14 and w >= 12: score -= 4
    return score

def laundry_advice(d: Dict, forecast: Dict) -> Dict[str, Any]:
    """
    Returns a laundry score (0-10) and a human-readable reason.
    """
    rain = forecast.get("rain_expected", 0)
    if rain > 0.1:
        return {"score": 0, "status": "no_laundry_rain", "reason": "Rain expected soon"}

    score = 5  # Base score for a dry day
    
    # --- Time of Day Factor ---
    # Best is morning/early afternoon (8:00 - 14:00)
    hour = time.localtime().tm_hour
    if 8 <= hour <= 13:
        score += 3
    elif 14 <= hour <= 16:
        score += 1
    elif hour >= 18 or hour < 6:
        score -= 4  # Night drying is slow unless very windy/warm

    # --- Wind Factor ---
    wind_speed = d.get("wind") or 0
    wind_name = d.get("wind_info", {}).get("name", "")
    
    if wind_name == "Bura":
        score += 3  # The king of drying
    elif wind_speed > 15:
        score += 2  # Good airflow
    elif wind_speed < 5:
        score -= 1  # Stagnant air

    # --- Warmth & Sun ---
    temp = d.get("air_temp") or 0
    sun = d.get("sun") or 0
    
    if temp > 25:
        score += 2
    if sun > 500:
        score += 2
    elif sun < 50 and hour < 17:
        score -= 1  # Overcast

    # --- Evening Exception ---
    # "evening is only if warm and windy and no rain"
    if hour >= 17:
        if temp > 20 and wind_speed > 12:
            score += 2  # Override some of the evening penalty
        else:
            score -= 2

    # Clamp score between 0 and 10
    final_score = max(0, min(10, score))
    
    # Classification
    if final_score >= 8:
        status = "excellent"
    elif final_score >= 5:
        status = "good"
    elif final_score >= 3:
        status = "marginal"
    else:
        status = "bad"

    return {
        "score": final_score,
        "status": status,
        "reason": f"Score {final_score}/10 ({wind_name if wind_name != 'local' else 'breeze'})"
    }

def bathing_feel(d: Dict) -> str:
    wt = d.get("water_temp")
    if wt is None: return "unknown"
    
    if wt < 11: t_f = "arctic shock"
    elif wt < 13: t_f = "extreme cold"
    elif wt < 16: t_f = "cold (nice)"
    elif wt < 19: t_f = "fresh"
    elif wt <= 22: t_f = "perfect"
    elif wt < 25: t_f = "warm"
    else: t_f = "too warm"

    w = d.get("wind") or 0
    w_f = "waves" if w >= 15 else "choppy" if w >= 8 else "flat"
    
    if wt >= 18 and w >= 12: return f"{t_f} + waves (fun, bit extreme)"
    if wt < 14 and w >= 12: return f"{t_f} + wind (not a good idea)"
    return f"{t_f} + {w_f}"

# -----------------------------
# CLI Actions
# -----------------------------

@app.command()
def status(
    limit: int = typer.Option(12, help="Number of trend samples"),
    timeout_sec: float = typer.Option(5.0, help="Request timeout"),
    json_out: bool = typer.Option(False, "--json"),
    pretty_json: bool = typer.Option(False, "--pretty-json"),
):
    start = now_ms()
    try:
        current = fetch_current(timeout_sec)
        warning = fetch_warning(timeout_sec)
        trend = fetch_trend(limit, timeout_sec)
        forecast = fetch_forecast(timeout_sec)
    except Exception as e:
        err("fetch_failed", details={"e": str(e)}, as_json=json_out)

    if warning:
        current["warning_type"] = warning.get("type")

    wind_info = classify_wind(current.get("wind_dir"))
    score = bathing_score(current)
    
    # 🧺 New Laundry Logic Integration
    laundry_data = laundry_advice({**current, "wind_info": wind_info}, forecast)
    
    temps = [x["temp"] for x in trend]
    levels = [x["level"] for x in trend]
    
    loc = "carinski_pomol" if current.get("wind_dir") in ("V", "SV") and (current.get("wind") or 0) >= 8 else "leseni_pomol"
    
    l_emoji = {"excellent": "🧺✨", "good": "🧺", "marginal": "☁️🧺", "bad": "🌧️❌"}.get(laundry_data["status"], "🧺")
    laundry_txt = f" | Laundry: {l_emoji} {laundry_data['status']} ({laundry_data['score']}/10)"

    summary = (
        f"{['bad','marginal','good','excellent'][max(0, (score+2)//3) if score > -900 else 0]} | "
        f"air={current['air_temp']}C water={current['water_temp']}C "
        f"wind={current['wind']}km/h {current['wind_dir']} ({wind_info['name']}) → {loc} "
        f"sea_temp={trend_direction(temps)} sea_level={trend_direction(levels)}/{detect_peak(levels)}"
        f"{laundry_txt}"
    )

    # ✅ Define 'out' BEFORE accessing it or adding extra keys
    out = {
        **current,
        "score": score,
        "location": loc,
        "wind_info": wind_info,
        "forecast": forecast,
        "laundry": laundry_data,  # Direct assignment here
        "feel": bathing_feel(current),
        "trends": {
            "temp": trend_direction(temps), 
            "level": trend_direction(levels), 
            "peak": detect_peak(levels)
        },
        "warning": warning,
        "latency_ms": now_ms() - start,
        "summary": summary,
    }

    ok(out, as_json=json_out, pretty=pretty_json)

@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, limit: int = 12, timeout_sec: float = 5.0, json_out: bool = False, pretty_json: bool = False):
    if ctx.invoked_subcommand is None:
        # Re-invoke status with the values, not the typer.Option objects
        status(limit=limit, timeout_sec=timeout_sec, json_out=json_out, pretty_json=pretty_json)

if __name__ == "__main__":
    app()
