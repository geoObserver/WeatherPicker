"""Weather Picker - Internationalisierung (DE/EN) und Locale-Formatierung.

Reines Daten-/Hilfsmodul ohne GUI- oder Netz-Abhaengigkeit, damit es auch in
Tests ohne laufendes QGIS importierbar ist.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QDate, QLocale


# =============================================================================
# Internationalisierung (DE/EN) – Kataloge liegen als locales/<lang>.json vor und
# sind ohne Code-Aenderung editierbar. Englisch ist gleichzeitig der Rueckfall.
# Eigenname "Weather Picker" bleibt in beiden Sprachen unuebersetzt.
# =============================================================================
_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")


def _load_catalog(lang: str) -> dict:
    """Uebersetzungs-Katalog ``locales/<lang>.json`` laden; fehlt/defekt ->
    leeres Dict (``tr()`` faellt dann auf Englisch zurueck)."""
    path = os.path.join(_LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


# Beim Import laden (winziger Datei-Read, einmalig).
TR = {lang: _load_catalog(lang) for lang in ("de", "en")}


def _current_lang() -> str:
    """Aktuelle Sprache (``"de"``/``"en"``) aus der QGIS-Oberflächensprache.

    ``QgsApplication.locale()`` folgt dem Override unter
    Einstellungen → Allgemein → Benutzeroberfläche. Alles außer Deutsch
    fällt auf Englisch zurück (``de_AT``/``de_CH`` → ``de``)."""
    code = (QgsApplication.locale() or "en")[:2].lower()
    return "de" if code == "de" else "en"


def tr(key: str, lang: str, **kwargs) -> str:
    """Übersetzten Text liefern; Englisch dient als Rückfall, falls ein
    Schlüssel in der gewählten Sprache fehlt."""
    template = TR.get(lang, TR["en"]).get(key) or TR["en"][key]
    return template.format(**kwargs) if kwargs else template


# --- Zahlen-/Datums-Locale -----------------------------------------------------
def _loc(lang: str) -> QLocale:
    """Zahlen-Locale, an die Textsprache gekoppelt: DE → Dezimalkomma,
    EN → Dezimalpunkt. (Über den Namen konstruiert, damit keine Qt5/Qt6-
    Enum-Unterschiede ins Spiel kommen.)"""
    return QLocale("de") if lang == "de" else QLocale("en")


def _fmt_num(value: float, decimals: int, lang: str) -> str:
    """Zahl mit dem Dezimaltrenner der Textsprache formatieren."""
    return _loc(lang).toString(float(value), "f", decimals)


def _date_locale(lang: str) -> QLocale:
    """Locale für die Tag/Monat-Reihenfolge im Datum.

    Folgt – anders als die Zahlen – dem *echten* Regions-Locale: liegt ein
    vollständiger Code vor (``en_GB``, ``en_US``, ``de_CH``), wird er exakt
    genutzt; bei reinem ``"en"`` entscheidet das System-Locale über die
    Reihenfolge (Monat/Tag in den USA, Tag/Monat in UK)."""
    name = QgsApplication.locale() or ""
    if "_" in name:
        return QLocale(name)
    if lang == "de":
        return QLocale("de")
    return QLocale.system()


def _short_md_format(loc: QLocale) -> str:
    """Aus dem Kurzdatums-Format des Locale die Tag/Monat-Anteile ableiten,
    indem das Jahr (samt angrenzender Trenner) entfernt wird.
    Bsp.: ``dd.MM.yyyy`` → ``dd.MM`` · ``M/d/yy`` → ``M/d`` · ``dd/MM/yyyy`` → ``dd/MM``."""
    fmt = loc.dateFormat(QLocale.FormatType.ShortFormat)
    fmt = re.sub(r"[^A-Za-z]*y+[^A-Za-z]*", "", fmt).strip(" ./-,")
    return fmt or "MM/dd"


def _format_datetime_label(dt: datetime.datetime, lang: str) -> str:
    """Datum MIT Jahr plus Uhrzeit, für die Bereichsangabe im Kopf.

    Anders als das Achsenlabel (``_format_date_label``), das ohne Jahr
    auskommt, weil die Achse einen zusammenhängenden Zeitraum zeigt: die
    Bereichsangabe steht auch im exportierten PNG und ist dort die einzige
    Stelle, an der überhaupt ein Jahr auftaucht.

    Das ``Uhr`` hinter der Zeit ist kein Anhängsel im Code, sondern Teil des
    Sprachkatalogs – im Englischen gibt es keine Entsprechung."""
    qd       = QDate(dt.year, dt.month, dt.day)
    lang_loc = _loc(lang)            # Wochentagsname in der Textsprache
    date_loc = _date_locale(lang)    # Reihenfolge/Jahresstellung nach Region
    wd = lang_loc.dayName(qd.dayOfWeek(), QLocale.FormatType.ShortFormat).rstrip(".")
    if lang == "de":
        datum = qd.toString("dd.MM.yyyy")
    else:
        datum = date_loc.toString(qd, date_loc.dateFormat(QLocale.FormatType.ShortFormat))
    # 24-Stunden-Schreibweise in beiden Sprachen, wie an der Zeitachse: eine
    # 12-Stunden-Form waere breiter und stuende neben 24-Stunden-Uhrzeiten.
    return tr("datetime_label", lang, wd=wd, d=datum, t=dt.strftime("%H:%M"))


def _format_date_label(dt: datetime.datetime, lang: str) -> str:
    """Achsen-Datumslabel à la ``Mo 09.06.`` (DE) bzw. ``Mon 06/09`` (EN).

    Wochentagsname folgt der Textsprache; die Tag/Monat-Reihenfolge folgt dem
    Regions-Locale (siehe ``_date_locale``). Beides kommt aus ``QLocale`` –
    keine hart kodierten Namenslisten mehr."""
    qd       = QDate(dt.year, dt.month, dt.day)
    lang_loc = _loc(lang)            # Wochentagsname in der Textsprache
    date_loc = _date_locale(lang)    # Reihenfolge/Monatsname nach Region
    # Trailing-Punkt mancher Locale-Kürzel ("Mo." → "Mo") für ein ruhiges Label entfernen.
    wd = lang_loc.dayName(qd.dayOfWeek(), QLocale.FormatType.ShortFormat).rstrip(".")
    if lang == "de":
        return f"{wd} {qd.toString('dd.MM.')}"
    return f"{wd} {date_loc.toString(qd, _short_md_format(date_loc))}"


# WMO-Wettercode (Open-Meteo-Feld ``weathercode``) → kurzer Klartext je Sprache.
# Tupel = (de, en). Nicht aufgeführte Codes liefern leeren String.
_WMO = {
    0:  ("klar", "clear"),
    1:  ("überwiegend klar", "mainly clear"),
    2:  ("teils bewölkt", "partly cloudy"),
    3:  ("bedeckt", "overcast"),
    45: ("Nebel", "fog"),
    48: ("gefrierender Nebel", "rime fog"),
    51: ("leichter Niesel", "light drizzle"),
    53: ("Niesel", "drizzle"),
    55: ("dichter Niesel", "dense drizzle"),
    56: ("gefrierender Niesel", "freezing drizzle"),
    57: ("gefrierender Niesel", "freezing drizzle"),
    61: ("leichter Regen", "light rain"),
    63: ("Regen", "rain"),
    65: ("starker Regen", "heavy rain"),
    66: ("gefrierender Regen", "freezing rain"),
    67: ("gefrierender Regen", "freezing rain"),
    71: ("leichter Schneefall", "light snow"),
    73: ("Schneefall", "snow"),
    75: ("starker Schneefall", "heavy snow"),
    77: ("Schneegriesel", "snow grains"),
    80: ("leichte Schauer", "light showers"),
    81: ("Schauer", "showers"),
    82: ("heftige Schauer", "violent showers"),
    85: ("leichte Schneeschauer", "light snow showers"),
    86: ("Schneeschauer", "snow showers"),
    95: ("Gewitter", "thunderstorm"),
    96: ("Gewitter mit Hagel", "thunderstorm with hail"),
    99: ("schweres Gewitter mit Hagel", "severe thunderstorm with hail"),
}


def weather_code_text(code, lang: str) -> str:
    """WMO-Wettercode in einen kurzen, lokalisierten Klartext übersetzen.

    Unbekannter/fehlender Code → leerer String (der Aufrufer blendet ihn dann
    einfach aus, statt einen Platzhalter zu zeigen)."""
    try:
        de, en = _WMO[int(code)]
    except (KeyError, TypeError, ValueError):
        return ""
    return de if lang == "de" else en
