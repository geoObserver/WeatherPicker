"""Weather Picker - Einstellungen (QgsSettings-Wrapper).

GUI-frei und ohne weitere Plugin-Abhaengigkeiten, damit es in Tests ohne
laufendes QGIS-UI nutzbar bleibt. Haelt Endpoint/Schluessel und die drei
Einheiten zentral; die Open-Meteo-Parameterwerte und die Anzeige-Symbole
liegen hier an EINER Stelle, damit URL-Bau (api.py) und Diagramm-Beschriftung
(chart.py) nicht auseinanderlaufen.
"""
from __future__ import annotations

from qgis.core import QgsSettings

_PREFIX = "WeatherPicker/"

# Vorgaben = bisheriges Verhalten (kostenloser Open-Meteo-Endpoint, metrisch).
DEFAULTS = {
    "base_url": "https://api.open-meteo.com/v1/forecast",
    "api_key": "",
    "temperature_unit": "celsius",   # celsius | fahrenheit
    "wind_unit": "kmh",              # kmh | ms | mph | kn  (Open-Meteo windspeed_unit)
    "precipitation_unit": "mm",      # mm | inch
    "collect_points": "false",       # "true"/"false": Ergebnisse in Sammel-Layer schreiben
    # Zwei abgeleitete Serien im Diagramm, einzeln abschaltbar. Sie liegen
    # ueber den Messwerten und sind nicht fuer jede Frage hilfreich - wer nur
    # den Tagesgang lesen will, schaltet sie weg. Vorgabe = an, wie bisher.
    "show_mean": "true",             # gleitendes 24-h-Mittel
    "show_cumulative": "true",       # kumulierter Niederschlag ab jetzt
    # Diagramm als Symbol am Punkt im Sammel-Layer. Schreibt je Abfrage eine
    # PNG-Datei neben das Projekt, deshalb abschaltbar.
    "chart_marker": "true",
}

# Erlaubte Werte je Einheit (Reihenfolge = Anzeige-Reihenfolge im Optionsdialog).
TEMP_UNITS = ("celsius", "fahrenheit")
WIND_UNITS = ("kmh", "ms", "mph", "kn")
PRECIP_UNITS = ("mm", "inch")

# Anzeige-Symbole je Einheitenwert (fuer Achsentitel + Bedingungszeile).
_TEMP_SYM = {"celsius": "°C", "fahrenheit": "°F"}
_WIND_SYM = {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "kn"}
_PRECIP_SYM = {"mm": "mm", "inch": "in"}


def get(key: str) -> str:
    """Einen Einstellungswert lesen; fehlt er, greift der Default."""
    value = QgsSettings().value(_PREFIX + key, DEFAULTS.get(key, ""))
    return str(value) if value is not None else DEFAULTS.get(key, "")


def set_value(key: str, value) -> None:
    QgsSettings().setValue(_PREFIX + key, value)


def all_settings() -> dict:
    """Alle Einstellungen als Dict (Schluessel wie in DEFAULTS)."""
    return {k: get(k) for k in DEFAULTS}


def flag(key: str) -> bool:
    """Einen Ja/Nein-Wert lesen. QgsSettings gibt je nach Backend "true",
    True oder 1 zurueck - deshalb ueber den Text vergleichen."""
    return str(get(key)).lower() in ("true", "1", "yes")


def set_flag(key: str, enabled: bool) -> None:
    set_value(key, "true" if enabled else "false")


def collect_points() -> bool:
    """True, wenn Ergebnisse in den Sammel-Layer geschrieben werden sollen."""
    return flag("collect_points")


def set_collect_points(enabled: bool) -> None:
    set_flag("collect_points", enabled)


def temp_symbol() -> str:
    return _TEMP_SYM.get(get("temperature_unit"), "°C")


def wind_symbol() -> str:
    return _WIND_SYM.get(get("wind_unit"), "km/h")


def precip_symbol() -> str:
    return _PRECIP_SYM.get(get("precipitation_unit"), "mm")


def unit_symbols() -> dict:
    """Anzeige-Symbole gebuendelt: {"temp": "°C", "wind": "km/h", "precip": "mm"}."""
    return {"temp": temp_symbol(), "wind": wind_symbol(), "precip": precip_symbol()}


# --- Helfer fuer den Optionsdialog -------------------------------------------
_ALLOWED = {
    "temperature_unit": TEMP_UNITS,
    "wind_unit": WIND_UNITS,
    "precipitation_unit": PRECIP_UNITS,
}
_SYMBOLS = {
    "temperature_unit": _TEMP_SYM,
    "wind_unit": _WIND_SYM,
    "precipitation_unit": _PRECIP_SYM,
}


def allowed(key: str) -> tuple:
    """Erlaubte Werte eines Einheiten-Settings (in Anzeige-Reihenfolge)."""
    return _ALLOWED.get(key, ())


def symbol_for(key: str, value: str) -> str:
    """Anzeige-Symbol fuer einen konkreten Einheitenwert (Fallback: der Wert selbst)."""
    return _SYMBOLS.get(key, {}).get(value, value)
