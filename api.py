"""Weather Picker - Netzabruf: Open-Meteo (Wetter) und Nominatim (Ortsname).

Beide Requests laufen ueber QgsBlockingNetworkRequest (Worker-Thread-tauglich,
respektiert QGIS-Proxy/Auth). fetch_weather wirft bei Fehlern lokalisierte
RuntimeError; reverse_geocode liefert bei jedem Fehler still None.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from . import settings
from .i18n import tr


def _plugin_version() -> str:
    """Plugin-Version aus ``metadata.txt`` lesen – Single Source of Truth.

    So driftet der User-Agent (Nominatim-Richtlinie verlangt eine Versions-
    angabe) nicht mehr von der tatsächlich ausgelieferten Version ab."""
    import configparser
    meta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt")
    try:
        cfg = configparser.ConfigParser()
        cfg.read(meta, encoding="utf-8")
        return cfg.get("general", "version", fallback="0.0")
    except Exception:
        return "0.0"


# Einmal beim Import bestimmt; in beiden Netz-Requests als User-Agent-Version genutzt.
PLUGIN_VERSION = _plugin_version()

# Modulweiter Ortsname-Cache (FIFO-begrenzt). Schluessel = auf ~100 m gerundete
# Koordinate; spart wiederholte Nominatim-Anfragen und haelt die Last gering.
_GEOCODE_CACHE: dict = {}
_GEOCODE_CACHE_MAX = 512


def _timeout_feedback(timeout_ms: int) -> tuple[QgsFeedback, QTimer]:
    """QgsFeedback + Einmal-Timer, der das Feedback nach ``timeout_ms`` abbricht.

    QgsBlockingNetworkRequest kennt keinen Timeout-Parameter und würde sonst bis
    zum globalen QGIS-Netzwerk-Timeout (Vorgabe 60 s) blockieren. Der Timer feuert
    während der internen Event-Loop von ``get()``; der Aufrufer muss ihn danach per
    ``timer.stop()`` anhalten, damit er bei schnellem Erfolg nicht später ins Leere
    feuert."""
    feedback = QgsFeedback()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(feedback.cancel)
    timer.start(timeout_ms)
    return feedback, timer


def _num(value):
    """Endliche Zahl zurückgeben oder None (Bool/NaN/inf/Strings → None).
    Damit der Renderer fehlende Felder einfach ausblenden kann."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _extract_extras(data: dict) -> dict:
    """Aktuelle Bedingungen (``current``) und Tageswerte (``daily``) robust aus der
    Antwort ziehen. Heute wird über das Datum aus ``current.time`` im
    ``daily.time``-Array gesucht (nicht Index 0 – wegen ``past_days`` läge dort ein
    vergangener Tag). Fehlt/ungültig → None.

    Der Rückgabeschlüssel ``daily`` trägt zusätzlich den vollständigen Tagesstreifen
    (alle von der API gelieferten Tage), damit das Diagramm ihn unter der
    Datumsachse zeichnen kann. Er stammt aus genau demselben ``daily``-Block wie
    ``today_*`` oben – so tragen Kopfzeile und Streifen für denselben Tag nie
    unterschiedliche Zahlen."""
    cur = data.get("current") if isinstance(data.get("current"), dict) else {}
    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}

    today_iso = str(cur.get("time") or "")[:10]
    dtimes = daily.get("time") if isinstance(daily.get("time"), list) else []
    idx = dtimes.index(today_iso) if today_iso in dtimes else None

    def day_val(key):
        arr = daily.get(key)
        if isinstance(arr, list) and idx is not None and 0 <= idx < len(arr):
            return _num(arr[idx])
        return None

    # Nur echte Datumsstrings behalten; tmin/tmax/psum bleiben zu dieser
    # gefilterten time-Liste index-synchron (ungültige Werte werden None,
    # NICHT übersprungen – sonst verrutscht die Zuordnung zum Datum).
    daily_time = [t for t in dtimes if isinstance(t, str)]

    def day_series(key):
        arr = daily.get(key) if isinstance(daily.get(key), list) else []
        return [
            _num(arr[i]) if i < len(arr) else None
            for i, t in enumerate(dtimes)
            if isinstance(t, str)
        ]

    return {
        "code":         cur.get("weathercode"),
        "temp":         _num(cur.get("temperature_2m")),
        "apparent":     _num(cur.get("apparent_temperature")),
        "humidity":     _num(cur.get("relativehumidity_2m")),
        "wind":         _num(cur.get("windspeed_10m")),
        "today_max":    day_val("temperature_2m_max"),
        "today_min":    day_val("temperature_2m_min"),
        "today_precip": day_val("precipitation_sum"),
        "daily": {
            "time":  daily_time,
            "tmin":  day_series("temperature_2m_min"),
            "tmax":  day_series("temperature_2m_max"),
            "psum":  day_series("precipitation_sum"),
        },
    }


def fetch_weather(
    lat: float, lon: float, lang: str = "de"
) -> tuple[list[str], list[float], list[float], int, dict]:
    # Endpoint und Einheiten aus den Plugin-Einstellungen (Default = bisheriges
    # Verhalten: kostenloser Open-Meteo-Endpoint, metrisch). Eigene Basis-URL +
    # API-Schluessel erlauben kommerzielle/self-hosted Nutzung.
    cfg = settings.all_settings()
    base = cfg["base_url"] or settings.DEFAULTS["base_url"]
    url = (
        f"{base}?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,precipitation"
        "&current=temperature_2m,apparent_temperature,relativehumidity_2m,"
        "windspeed_10m,weathercode"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&temperature_unit={cfg['temperature_unit']}"
        f"&windspeed_unit={cfg['wind_unit']}"
        f"&precipitation_unit={cfg['precipitation_unit']}"
        "&past_days=2&forecast_days=7&timezone=auto"
    )
    if cfg["api_key"]:
        url += f"&apikey={cfg['api_key']}"

    # Über den QGIS-Netzwerk-Manager statt über die Python-Bibliothek `requests`
    # anfragen: nur so werden die QGIS-/System-Proxy-Einstellungen inkl.
    # Authentifizierung (z. B. NTLM/Kerberos im Firmennetz) berücksichtigt.
    request = QNetworkRequest(QUrl(url))
    request.setHeader(
        QNetworkRequest.KnownHeaders.UserAgentHeader,
        f"QGIS-WeatherPicker/{PLUGIN_VERSION}",
    )

    # Eigener 15-s-Timeout (siehe _timeout_feedback).
    feedback, timer = _timeout_feedback(15000)

    blocking = QgsBlockingNetworkRequest()

    # Läuft im Worker-Thread eines QgsTask (siehe WeatherFetchTask): der blockierende
    # get()-Aufruf hält damit nur den Hintergrund-Thread an, nicht die GUI.
    # QgsBlockingNetworkRequest ist ausdrücklich für die Nutzung in Worker-Threads
    # vorgesehen. Kein Cursor-Override mehr (GUI-Zugriff aus dem Worker wäre unzulässig);
    # die Lade-Rückmeldung übernimmt der QgsTask-Manager bzw. die Message-Bar.
    try:
        # forceRefresh=True: keine gecachte Antwort verwenden – Wetterdaten
        # sollen aktuell sein (sonst liefert der QGIS-Cache evtl. alte Werte).
        err = blocking.get(request, True, feedback)  # request, forceRefresh, feedback
    finally:
        timer.stop()  # nicht mehr benötigt, egal ob Erfolg/Fehler/Timeout

    if feedback.isCanceled():
        raise RuntimeError(tr("err_timeout", lang))

    # HTTP-Fehler (4xx/5xx) liefert get() bereits als ServerExceptionError zurück.
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise RuntimeError(tr("err_network", lang, msg=blocking.errorMessage()))

    content = bytes(blocking.reply().content()).decode("utf-8")

    # --- Antwort strukturiert prüfen ---------------------------------------
    # json.JSONDecodeError ist eine ValueError-Unterklasse, daher mit abgedeckt.
    try:
        data   = json.loads(content)
        hourly = data["hourly"]
        times  = hourly["time"]
        temp   = hourly["temperature_2m"]
        precip = hourly["precipitation"]
    except KeyError as exc:
        # Ein fehlendes Feld ist der wahrscheinliche Fall bei abweichender
        # base_url (self-hosted oder aeltere Open-Meteo-Instanz, die etwa
        # "precipitation" noch nicht kennt). Den Feldnamen nennen: sonst ist
        # das vom echten Parser-Fehler nicht zu unterscheiden und der
        # Anwender sucht an der falschen Stelle.
        raise RuntimeError(tr("err_field_missing", lang,
                              field=exc.args[0] if exc.args else "?",
                              url=base)) from exc
    except (ValueError, TypeError) as exc:
        raise RuntimeError(tr("err_format", lang)) from exc

    # Alle drei Felder müssen Listen sein (nicht nur "times").
    if not (isinstance(times, list) and isinstance(temp, list) and isinstance(precip, list)):
        raise RuntimeError(tr("err_format", lang))
    if not (len(times) == len(temp) == len(precip)):
        raise RuntimeError(tr("err_inconsistent", lang))

    # utc_offset_seconds liefert Open-Meteo bei timezone=auto mit. Damit lässt
    # sich die lokale "Jetzt"-Zeit am Ort bestimmen, ohne auf die hourly-Indizes
    # angewiesen zu sein (die durch das null-Filtern verschoben sein könnten).
    utc_offset = data.get("utc_offset_seconds", 0)
    if not isinstance(utc_offset, (int, float)) or isinstance(utc_offset, bool):
        utc_offset = 0

    # Strikte Wert-Validierung: Open-Meteo liefert in Randlagen (Polarregion,
    # offene See) teils null-Werte; theoretisch könnten auch Strings/Booleans/
    # NaN/inf auftreten. Nur endliche Zahlen behalten, negativen Niederschlag
    # (Artefakt) auf 0 klemmen. Die drei Listen bleiben dabei index-synchron.
    cleaned = []
    for tm, tp, rn in zip(times, temp, precip):
        if not isinstance(tm, str):
            continue
        if isinstance(tp, bool) or isinstance(rn, bool):
            continue
        if not isinstance(tp, (int, float)) or not isinstance(rn, (int, float)):
            continue
        if not (math.isfinite(tp) and math.isfinite(rn)):
            continue
        if rn < 0:
            rn = 0.0
        cleaned.append((tm, float(tp), float(rn)))

    if not cleaned:
        raise RuntimeError(tr("err_nodata", lang))

    times, temp, precip = (list(col) for col in zip(*cleaned))
    # Zusatzinfos (aktuelle Bedingungen + Tageswerte) für die Bedingungszeile.
    extras = _extract_extras(data)
    return times, temp, precip, utc_offset, extras


def _pick_place_name(address: dict) -> str | None:
    """Aus dem Nominatim-``address``-Block den sinnvollsten Ortsnamen wählen.

    Fallback-Kette von fein (Stadt/Ort) zu grob (Kreis/Region): so kommt auch
    in dünn besiedelten Gebieten oder am Stadtrand noch ein brauchbarer Name
    heraus. Der erste nicht-leere Treffer gewinnt."""
    if not isinstance(address, dict):
        return None
    for feld in (
        "city", "town", "village", "hamlet", "municipality",
        "suburb", "city_district", "county", "state",
    ):
        wert = address.get(feld)
        if isinstance(wert, str) and wert.strip():
            return wert.strip()
    return None


# Nominatim erlaubt hoechstens eine Anfrage pro Sekunde. Bisher wurde das nur
# zufaellig durch die Netzlatenz eingehalten - bei einer Ortssuche folgen
# forward_geocode und reverse_geocode unmittelbar aufeinander. Der Zeitstempel
# ist modulweit, weil das Limit fuer den Dienst gilt und nicht pro Aufrufpfad.
# 1,1 statt 1,0 Sekunden: time.sleep kehrt unter Windows wegen der
# Timer-Granularitaet von rund 15 ms zu frueh zurueck - gemessen 0,985 s
# Abstand bei einem Sollwert von 1,0. Das laege unter dem Limit. Die
# Marge kostet 100 ms und stellt die Einhaltung sicher.
_NOMINATIM_ABSTAND_S = 1.1
_nominatim_sperre = threading.Lock()
_nominatim_zuletzt = 0.0


def _nominatim_drosseln() -> None:
    """Bis zum erlaubten Abstand zur letzten Nominatim-Anfrage warten.

    Das Warten blockiert absichtlich: beide Aufrufer sitzen in
    ``WeatherFetchTask.run`` und damit im Worker-Thread, wo eine Sekunde
    Schlaf die Oberflaeche nicht anhaelt. Der Zeitstempel wird vor dem
    Absenden gesetzt, denn die Richtlinie begrenzt die Anfragerate, also
    den Abstand von Start zu Start."""
    global _nominatim_zuletzt
    with _nominatim_sperre:
        warte = _NOMINATIM_ABSTAND_S - (time.monotonic() - _nominatim_zuletzt)
        if warte > 0:
            time.sleep(warte)
        _nominatim_zuletzt = time.monotonic()


def _nominatim_request(url: str, accept_lang: str) -> QNetworkRequest:
    """QNetworkRequest für Nominatim mit dem von der Nutzungsrichtlinie verlangten,
    aussagekräftigen User-Agent (identifiziert die Anwendung; sonst droht Sperre)
    und passender Accept-Language. Von reverse_geocode und forward_geocode geteilt.

    Haelt zugleich den vorgeschriebenen Mindestabstand zur vorigen Anfrage ein."""
    _nominatim_drosseln()
    request = QNetworkRequest(QUrl(url))
    request.setHeader(
        QNetworkRequest.KnownHeaders.UserAgentHeader,
        f"QGIS-WeatherPicker/{PLUGIN_VERSION} "
        "(+https://github.com/geoObserver/WeatherPicker; news@geoobserver.de)",
    )
    request.setRawHeader(b"Accept-Language", accept_lang.encode("ascii"))
    return request


def reverse_geocode(lat: float, lon: float, lang: str = "de") -> str | None:
    """Nächstgelegenen Ortsnamen (Stadt/Gemeinde/Ort) via Nominatim ermitteln.

    Läuft – wie ``fetch_weather`` – bewusst über den QGIS-Netzwerk-Manager,
    damit die in QGIS hinterlegten Proxy-/Authentifizierungseinstellungen
    (z. B. NTLM/Kerberos im Firmennetz) greifen.

    Optionales Feature: Jeder Fehler (Netz, Timeout, kein Treffer, ungültige
    Antwort) führt absichtlich zu ``None`` statt zu einer Ausnahme – dann zeigt
    das Diagramm einfach nur die Koordinaten, ohne Fehlermeldung im UI.

    Datenschutz: Wie beim Wetterabruf werden die exakten Koordinaten an einen
    Drittanbieter übertragen – hier OpenStreetMap/Nominatim.
    """
    # Cache-Schlüssel auf ~100 m runden: feiner braucht "nächster Ort" nicht,
    # und identische bzw. minimal verschobene Klicks treffen denselben Eintrag.
    # Auch ein None-Ergebnis (offene See o. ä.) wird gecacht, damit derselbe
    # Punkt nicht erneut angefragt wird.
    key = (round(lat, 3), round(lon, 3))
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]

    # accept-language an die Oberflächensprache koppeln → deutsche Ortsnamen.
    accept_lang = "de" if lang == "de" else "en"
    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}"
        "&format=jsonv2&zoom=12&addressdetails=1"
        f"&accept-language={accept_lang}"
    )

    request = _nominatim_request(url, accept_lang)

    # Eigener Timeout wie beim Wetterabruf, aber kürzer (8 s): das Geocoding ist
    # optional und soll den Ablauf nicht spürbar verzögern.
    feedback, timer = _timeout_feedback(8000)

    blocking = QgsBlockingNetworkRequest()
    try:
        # forceRefresh=False: Ortsnamen ändern sich praktisch nie, daher darf
        # die QGIS-Cache-Antwort genutzt werden – das entlastet den Dienst.
        err = blocking.get(request, False, feedback)  # request, forceRefresh, feedback
    finally:
        timer.stop()

    # Bei Timeout/Netzfehler nicht cachen, damit ein späterer Versuch (z. B.
    # nach Netzwiederkehr) denselben Punkt erneut anfragen darf.
    if feedback.isCanceled() or err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        return None

    try:
        content = bytes(blocking.reply().content()).decode("utf-8")
        data    = json.loads(content)
        address = data.get("address", {}) if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return None

    place = _pick_place_name(address)
    # FIFO-Eviction: ältesten Eintrag verwerfen, bevor das Limit überschritten
    # wird (Python-Dicts behalten die Einfügereihenfolge → erster Key = ältester).
    if len(_GEOCODE_CACHE) >= _GEOCODE_CACHE_MAX:
        _GEOCODE_CACHE.pop(next(iter(_GEOCODE_CACHE)), None)
    _GEOCODE_CACHE[key] = place
    return place


def forward_geocode(query: str, lang: str = "de") -> tuple[float, float, str] | None:
    """Ort/Adresse per Freitext suchen (Nominatim ``/search``).

    Liefert ``(lat, lon, label)`` des besten Treffers oder ``None`` (kein Treffer,
    Netz-/Timeout-Fehler, leere Eingabe). Wie ``reverse_geocode`` schlägt jeder
    Fehler still auf ``None`` fehl – der Aufrufer zeigt dann nur einen Hinweis."""
    q = (query or "").strip()
    if not q:
        return None

    accept_lang = "de" if lang == "de" else "en"
    q_enc = bytes(QUrl.toPercentEncoding(q)).decode("ascii")
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={q_enc}"
        "&format=jsonv2&limit=1&addressdetails=0"
        f"&accept-language={accept_lang}"
    )

    feedback, timer = _timeout_feedback(8000)
    blocking = QgsBlockingNetworkRequest()
    try:
        err = blocking.get(_nominatim_request(url, accept_lang), False, feedback)
    finally:
        timer.stop()

    if feedback.isCanceled() or err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        return None

    try:
        data = json.loads(bytes(blocking.reply().content()).decode("utf-8"))
        if not isinstance(data, list) or not data:
            return None
        hit = data[0]
        lat = float(hit["lat"])
        lon = float(hit["lon"])
        label = hit.get("display_name") or q
    except (ValueError, KeyError, TypeError):
        return None

    return lat, lon, str(label)
