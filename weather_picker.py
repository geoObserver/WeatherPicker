"""
Weather Picker - QGIS-Plugin
============================

Auf die Karte klicken und Wetterdaten inkl. 7-Tage-Vorschau (Open-Meteo)
als Diagramm anzeigen.

Der Netzwerkabruf läuft bewusst über den QGIS-Netzwerk-Manager
(``QgsBlockingNetworkRequest``) statt über ``requests``, damit die in QGIS
hinterlegten Proxy-/Authentifizierungs-Einstellungen (z. B. NTLM/Kerberos im
Firmennetz) berücksichtigt werden.

Sprache: Die Oberfläche (Texte, Diagramm-Beschriftungen, Datums-/Zahlenformate)
folgt automatisch der QGIS-Oberflächensprache – Deutsch, sonst Englisch als
Rückfall (siehe ``_current_lang``).

Datenschutz: Beim Klick werden die Koordinaten der angeklickten Position an
Open-Meteo (open-meteo.com) übertragen. Ins QGIS-Log werden Koordinaten nur
gerundet (~1 km) geschrieben. Siehe README.
"""

from __future__ import annotations  # Type Hints als Strings → 3.16-kompatibel

import datetime
import json
import math
import os
import re

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeedback,
    QgsMessageLog,
    QgsProject,
)
from qgis.gui import QgsMapTool
from qgis.PyQt import QtCore, QtGui, QtWidgets
from qgis.PyQt.QtCore import QDate, QLocale, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

# =============================================================================
# Qt5 / Qt6 Enum-Kompatibilität (QGIS 3 & QGIS 4)
# =============================================================================
if not hasattr(QtGui.QImage, "Format_ARGB32") or not hasattr(Qt, "AlignCenter"):
    # QImage
    QtGui.QImage.Format_ARGB32        = QtGui.QImage.Format.Format_ARGB32
    # QPainter
    QtGui.QPainter.Antialiasing       = QtGui.QPainter.RenderHint.Antialiasing
    QtGui.QPainter.TextAntialiasing   = QtGui.QPainter.RenderHint.TextAntialiasing
    # QFont
    QtGui.QFont.Bold                  = QtGui.QFont.Weight.Bold
    QtGui.QFont.Normal                = QtGui.QFont.Weight.Normal
    # Qt Alignment
    QtCore.Qt.AlignLeft               = QtCore.Qt.AlignmentFlag.AlignLeft
    QtCore.Qt.AlignRight              = QtCore.Qt.AlignmentFlag.AlignRight
    QtCore.Qt.AlignCenter             = QtCore.Qt.AlignmentFlag.AlignCenter
    QtCore.Qt.AlignVCenter            = QtCore.Qt.AlignmentFlag.AlignVCenter
    QtCore.Qt.AlignHCenter            = QtCore.Qt.AlignmentFlag.AlignHCenter
    # Qt PenStyle
    QtCore.Qt.DashLine                = QtCore.Qt.PenStyle.DashLine
    QtCore.Qt.SolidLine               = QtCore.Qt.PenStyle.SolidLine
    # Qt Pen cap/join (für die geglättete Kurve)
    QtCore.Qt.RoundCap                = QtCore.Qt.PenCapStyle.RoundCap
    QtCore.Qt.RoundJoin               = QtCore.Qt.PenJoinStyle.RoundJoin
    # Qt TransformationMode
    QtCore.Qt.SmoothTransformation    = QtCore.Qt.TransformationMode.SmoothTransformation
    # Qt MouseButton
    QtCore.Qt.LeftButton              = QtCore.Qt.MouseButton.LeftButton
    # Qt CursorShape
    QtCore.Qt.WaitCursor              = QtCore.Qt.CursorShape.WaitCursor


# Einheitlicher Log-Tag → im QGIS-Log-Panel als eigener Reiter filterbar.
LOG_TAG = "Weather Picker"

# =============================================================================
# Internationalisierung (DE/EN) – leichtgewichtige Dict-Tabelle, kein Build-Tooling.
# Schlüssel → Sprache → Vorlage. Englisch ist gleichzeitig der Rückfall.
# Eigenname "Weather Picker" bleibt in beiden Sprachen unübersetzt.
# =============================================================================
TR = {
    "de": {
        "action_tooltip":  "Weather Picker – Auf Karte klicken für Wetterdaten",
        "icon_missing":    "Icon nicht gefunden: {path} – Ausweich-Icon wird verwendet.",
        "click_hint":      "Auf die Karte klicken – Wetterdaten & 7-Tage-Vorschau als Diagramm",
        "err_timeout":     "Zeitüberschreitung (15 s) beim Abruf der Wetterdaten – "
                           "Netzwerk oder Proxy nicht erreichbar?",
        "err_network":     "Netzwerkfehler beim Abruf der Wetterdaten: {msg}",
        "err_format":      "Unerwartetes Antwortformat der Open-Meteo-API.",
        "err_inconsistent":"Inkonsistente Wetterdaten von der API erhalten.",
        "err_nodata":      "Keine gültigen Wetterdaten für diese Position verfügbar.",
        "err_crs":         "Ungültiges Koordinatensystem – Transformation nicht möglich.",
        "chart_title":     "Wetterdaten & 7-Tage-Vorschau",
        "coords":          "Breite {lat}, Länge {lon}",
        "coords_near":     "Breite {lat}, Länge {lon} – in der Nähe von {place}",
        "now":             "jetzt",
        "temp_axis":       "Temperatur (°C)",
        "rain_axis":       "Regen (mm)",
        "legend_temp":     "Temperatur",
        "legend_rain":     "Regen",
        "source":          '<a href="https://open-meteo.com">Wetterdaten/Vorhersage: '
                           'Open-Meteo</a> – Lizenz '
                           '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>',
        "source_osm":      'Ortsname: © <a href="https://www.openstreetmap.org/copyright">'
                           'OpenStreetMap</a>-Mitwirkende '
                           '(<a href="https://nominatim.openstreetmap.org/">Nominatim</a>)',
        "log_release":     "canvasReleaseEvent ausgelöst",
        "log_coords":      "Koordinaten (gerundet): lat={lat}, lon={lon}",
        "log_received":    "Wetterdaten empfangen: {n} Einträge",
        "log_place":       "Nächster Ort (Nominatim): {place}",
        "log_dialog_open": "Dialog wird geöffnet...",
        "log_dialog_closed":"Dialog geschlossen",
    },
    "en": {
        "action_tooltip":  "Weather Picker – click the map for weather data",
        "icon_missing":    "Icon not found: {path} – using fallback icon.",
        "click_hint":      "Click the map – weather data & 7-day forecast as a chart",
        "err_timeout":     "Request timed out (15 s) while fetching weather data – "
                           "network or proxy unreachable?",
        "err_network":     "Network error while fetching weather data: {msg}",
        "err_format":      "Unexpected response format from the Open-Meteo API.",
        "err_inconsistent":"Inconsistent weather data received from the API.",
        "err_nodata":      "No valid weather data available for this location.",
        "err_crs":         "Invalid coordinate reference system – transformation not possible.",
        "chart_title":     "Weather data & 7-day forecast",
        "coords":          "Lat {lat}, Lon {lon}",
        "coords_near":     "Lat {lat}, Lon {lon} – near {place}",
        "now":             "now",
        "temp_axis":       "Temperature (°C)",
        "rain_axis":       "Rain (mm)",
        "legend_temp":     "Temperature",
        "legend_rain":     "Rain",
        "source":          '<a href="https://open-meteo.com">Weather data/forecast: '
                           'Open-Meteo</a> – licensed '
                           '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>',
        "source_osm":      'Place name: © <a href="https://www.openstreetmap.org/copyright">'
                           'OpenStreetMap</a> contributors '
                           '(<a href="https://nominatim.openstreetmap.org/">Nominatim</a>)',
        "log_release":     "canvasReleaseEvent triggered",
        "log_coords":      "Coordinates (rounded): lat={lat}, lon={lon}",
        "log_received":    "Weather data received: {n} entries",
        "log_place":       "Nearest place (Nominatim): {place}",
        "log_dialog_open": "Opening dialog...",
        "log_dialog_closed":"Dialog closed",
    },
}


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


def _log(message: object, level: int = Qgis.Info) -> None:
    """Status-/Debug-Ausgabe ins QGIS-Log-Panel statt auf stdout."""
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)


def _schoener_schritt(spanne: float, anzahl: int) -> float:
    """Liefert einen "schönen" Achsen-Schritt (1/2/5 × Zehnerpotenz) für `anzahl`
    Intervalle über die gegebene Wertespanne – für lesbare, gerundete Achsen."""
    if spanne <= 0:
        return 1.0
    roh = spanne / max(anzahl, 1)
    mag = 10 ** math.floor(math.log10(roh))
    norm = roh / mag
    if norm < 1.5:
        s = 1
    elif norm < 3:
        s = 2
    elif norm < 7:
        s = 5
    else:
        s = 10
    return s * mag


# =============================================================================
# Plugin-Klasse
# =============================================================================
class WeatherPickerPlugin:
    """Plugin-Lebenszyklus: Toolbar/Action anlegen, Map-Tool umschalten, aufräumen."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.toolbar = None
        self._owns_toolbar = False  # nur selbst erzeugte Toolbar darf aufgeräumt werden
        self.action = None
        self.actions = []
        self.tool = None  # erst in activate_tool() belegt; hält das aktive Map-Tool

    def initGui(self) -> None:
        lang = _current_lang()

        # --- Toolbar "geoObserverTools" suchen oder neu anlegen ---
        # Die Toolbar wird von mehreren geoObserver-Plugins geteilt. Eigentümerschaft
        # merken, damit wir beim Entladen keine fremde Toolbar zerstören.
        self.toolbar = self.iface.mainWindow().findChild(
            QtWidgets.QToolBar, "geoObserverTools"
        )

        if not self.toolbar:
            self.toolbar = self.iface.addToolBar("geoObserverTools")
            self.toolbar.setObjectName("geoObserverTools")
            self._owns_toolbar = True

        # --- Icon laden (logo.png liegt im gleichen Ordner wie dieses Skript) ---
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path  = os.path.join(plugin_dir, "logo.png")

        if os.path.exists(icon_path):
            icon = QtGui.QIcon(icon_path)
        else:
            # Ausweich-Icon: Standard-QGIS-Icon, falls logo.png nicht gefunden wird
            icon = QtGui.QIcon(":/images/themes/default/mActionIdentify.svg")
            self.iface.messageBar().pushMessage(
                "Weather Picker",
                tr("icon_missing", lang, path=icon_path),
                level=Qgis.Warning
            )

        # --- Button / Action anlegen ---
        self.action = QtWidgets.QAction(icon, "Weather Picker", self.iface.mainWindow())
        self.action.setToolTip(tr("action_tooltip", lang))
        self.action.setCheckable(True)
        self.action.triggered.connect(self.activate_tool)

        self.toolbar.addAction(self.action)
        self.actions.append(self.action)

    def activate_tool(self, checked: bool = False) -> None:
        # Umschaltbare Action: Bei erneutem Klick (checked=False) das Werkzeug
        # wieder abwählen, statt ein neues Map-Tool zu setzen.
        if not checked:
            if self.tool is not None and self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            return

        self.tool = WeatherPickerTool(self.iface, self.canvas, self.action)
        self.canvas.setMapTool(self.tool)

        self.iface.messageBar().pushMessage(
            "Weather Picker",
            tr("click_hint", _current_lang()),
            level=Qgis.Info
        )

    def unload(self) -> None:
        # Aktives Map-Tool zurücksetzen, falls noch unseres aktiv ist –
        # sonst bleibt es als Zombie-Referenz im Canvas hängen.
        if self.tool is not None and self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        self.tool = None

        # Nur die eigenen Actions entfernen. Die geteilte Toolbar wird NICHT per
        # removeToolBar() angefasst: Qt würde sie nur verstecken, beim nächsten
        # Laden fände findChild() eine versteckte, nicht wieder eingehängte Toolbar
        # (klassische "Toolbar weg nach Reload"-Falle).
        for a in self.actions:
            if self.toolbar is not None:
                self.toolbar.removeAction(a)
            a.deleteLater()
        self.actions = []
        self.action = None

        # Haben wir die Toolbar selbst erzeugt und ist sie jetzt leer, geben wir
        # sie sauber frei (deleteLater statt removeToolBar, um die Verstecken-Falle
        # zu umgehen). Eine fremde/geteilte Toolbar bleibt unangetastet.
        if self._owns_toolbar and self.toolbar is not None and len(self.toolbar.actions()) == 0:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        self.toolbar = None
        self._owns_toolbar = False


# =============================================================================
# Map-Tool-Klasse
# =============================================================================
class WeatherPickerTool(QgsMapTool):

    # Sitzungsweiter Ortsname-Cache (Klassenattribut → überlebt das Neu-Anlegen
    # des Map-Tools bei jedem Aktivieren). Schlüssel = auf ~100 m gerundete
    # Koordinate. Spart wiederholte Nominatim-Anfragen für denselben Punkt und
    # hält die Last gering – Nominatim erlaubt max. 1 Anfrage/Sekunde.
    _geocode_cache: dict = {}

    def __init__(self, iface, canvas, action: QtWidgets.QAction | None = None) -> None:
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self.action = action

    def deactivate(self) -> None:
        """Wird aufgerufen, wenn ein anderes Werkzeug aktiviert wird."""
        if self.action:
            self.action.setChecked(False)
        super().deactivate()

    # -------------------------------------------------------------------------
    def fetch_weather(
        self, lat: float, lon: float, lang: str = "de"
    ) -> tuple[list[str], list[float], list[float], int]:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,rain"
            "&past_days=2&forecast_days=7&timezone=auto"
        )

        # Über den QGIS-Netzwerk-Manager statt über die Python-Bibliothek `requests`
        # anfragen: nur so werden die QGIS-/System-Proxy-Einstellungen inkl.
        # Authentifizierung (z. B. NTLM/Kerberos im Firmennetz) berücksichtigt.
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "QGIS-WeatherPicker")

        # QgsBlockingNetworkRequest kennt keinen Timeout-Parameter und würde sonst
        # bis zum globalen QGIS-Netzwerk-Timeout (Vorgabe 60 s) blockieren. Über ein
        # QgsFeedback + Einmal-Timer brechen wir nach 15 s selbst ab. Der Timer feuert
        # während der internen Event-Loop von get() und wird danach gestoppt, damit er
        # bei schnellem Erfolg nicht später noch ins Leere feuert.
        feedback = QgsFeedback()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(feedback.cancel)
        timer.start(15000)

        blocking = QgsBlockingNetworkRequest()

        # Hinweis: Der Aufruf blockiert den UI-Thread (bis zu 15 s). Architektonisch
        # sauber wäre ein QgsTask + Signal/Slot mit Ladeanzeige – das ist aber ein
        # größerer Umbau. Als minimale Rückmeldung wenigstens einen Warte-Cursor zeigen.
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.WaitCursor))
        try:
            # forceRefresh=True: keine gecachte Antwort verwenden – Wetterdaten
            # sollen aktuell sein (sonst liefert der QGIS-Cache evtl. alte Werte).
            err = blocking.get(request, True, feedback)  # request, forceRefresh, feedback
        finally:
            timer.stop()  # nicht mehr benötigt, egal ob Erfolg/Fehler/Timeout
            QtWidgets.QApplication.restoreOverrideCursor()

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
            rain   = hourly["rain"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(tr("err_format", lang)) from exc

        # Alle drei Felder müssen Listen sein (nicht nur "times").
        if not (isinstance(times, list) and isinstance(temp, list) and isinstance(rain, list)):
            raise RuntimeError(tr("err_format", lang))
        if not (len(times) == len(temp) == len(rain)):
            raise RuntimeError(tr("err_inconsistent", lang))

        # utc_offset_seconds liefert Open-Meteo bei timezone=auto mit. Damit lässt
        # sich die lokale "Jetzt"-Zeit am Ort bestimmen, ohne auf die hourly-Indizes
        # angewiesen zu sein (die durch das null-Filtern verschoben sein könnten).
        utc_offset = data.get("utc_offset_seconds", 0)
        if not isinstance(utc_offset, (int, float)) or isinstance(utc_offset, bool):
            utc_offset = 0

        # Strikte Wert-Validierung: Open-Meteo liefert in Randlagen (Polarregion,
        # offene See) teils null-Werte; theoretisch könnten auch Strings/Booleans/
        # NaN/inf auftreten. Nur endliche Zahlen behalten, negativen Regen (Artefakt)
        # auf 0 klemmen. Die drei Listen bleiben dabei index-synchron.
        cleaned = []
        for tm, tp, rn in zip(times, temp, rain):
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

        times, temp, rain = (list(col) for col in zip(*cleaned))
        return times, temp, rain, utc_offset

    # -------------------------------------------------------------------------
    @staticmethod
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

    def reverse_geocode(self, lat: float, lon: float, lang: str = "de") -> str | None:
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
        if key in self._geocode_cache:
            return self._geocode_cache[key]

        # accept-language an die Oberflächensprache koppeln → deutsche Ortsnamen.
        accept_lang = "de" if lang == "de" else "en"
        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?lat={lat}&lon={lon}"
            "&format=jsonv2&zoom=12&addressdetails=1"
            f"&accept-language={accept_lang}"
        )

        request = QNetworkRequest(QUrl(url))
        # Nominatim-Nutzungsrichtlinie verlangt einen aussagekräftigen User-Agent,
        # der die Anwendung identifiziert – sonst droht eine Sperre. Kontakt/Repo
        # aus den Plugin-Metadaten.
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            "QGIS-WeatherPicker/0.4 (+https://github.com/geoObserver/weather_picker; "
            "news@geoobserver.de)",
        )
        request.setRawHeader(b"Accept-Language", accept_lang.encode("ascii"))

        # Eigener Timeout wie beim Wetterabruf, aber kürzer (8 s): das Geocoding ist
        # optional und soll den Ablauf nicht spürbar verzögern.
        feedback = QgsFeedback()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(feedback.cancel)
        timer.start(8000)

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

        place = self._pick_place_name(address)
        self._geocode_cache[key] = place
        return place

    # -------------------------------------------------------------------------
    def create_png(
        self,
        lat: float,
        lon: float,
        times: list[str],
        temp: list[float],
        rain: list[float],
        now_local: datetime.datetime | None = None,
        scale: float = 1.0,
        lang: str = "de",
        place: str | None = None,
    ) -> QtGui.QImage:

        # --- Farbpalette (dezent-modern, aber kontraststark) ---
        C_INK       = QtGui.QColor("#222222")          # Haupttext
        C_MUTE      = QtGui.QColor("#6f6f6f")          # Sekundärtext / Datum
        C_GRID      = QtGui.QColor("#e3e3e3")          # Gitternetz
        C_AXIS      = QtGui.QColor("#bdbdbd")          # Achsenlinien
        C_NOW       = QtGui.QColor("#3a3a3a")          # "Jetzt"-Linie
        C_PAST      = QtGui.QColor(0, 0, 0, 12)        # Schattierung Vergangenheit
        C_TEMP      = QtGui.QColor("#e4572e")          # Temperatur (warmes Rot-Orange)
        C_RAIN      = QtGui.QColor(48, 127, 226, 190)  # Regenbalken (kräftiges Blau)
        C_RAIN_INK  = QtGui.QColor("#1d6fb8")          # Regen-Beschriftung

        # Logische Zeichenfläche; physisch wird mit `scale` (Geräte-Pixeldichte)
        # gerendert, damit nichts heruntergerechnet (= unscharf) werden muss.
        width, height = 1280, 620
        if scale < 1.0:
            scale = 1.0

        img = QtGui.QImage(
            max(1, int(width * scale)),
            max(1, int(height * scale)),
            QtGui.QImage.Format_ARGB32,
        )
        img.fill(QtGui.QColor("white"))

        painter = QtGui.QPainter(img)
        # try/finally: bei einer Exception während des Zeichnens muss painter.end()
        # trotzdem laufen, sonst bleibt das QImage-Backend gesperrt (Ressourcen-Leck).
        try:
            painter.scale(scale, scale)  # in logischen Koordinaten zeichnen
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setRenderHint(QtGui.QPainter.TextAntialiasing)

            # --- Abstände (Ränder) ---
            margin_left   = 82
            margin_right  = 82
            margin_top    = 78
            margin_bottom = 72

            plot_w      = width  - margin_left - margin_right
            plot_h      = height - margin_top  - margin_bottom
            plot_bottom = height - margin_bottom

            n = len(temp)

            # --- Temperaturachse: schön gerundete Grenzen + etwas Luft ---
            t_lo, t_hi = min(temp), max(temp)
            if t_hi - t_lo < 0.5:          # sehr flache Kurve nicht übermäßig zoomen
                mid = (t_hi + t_lo) / 2.0
                t_lo, t_hi = mid - 0.5, mid + 0.5
            step_t   = _schoener_schritt(t_hi - t_lo, 6)
            axis_min = math.floor(t_lo / step_t) * step_t
            axis_max = math.ceil(t_hi / step_t) * step_t
            n_ticks  = max(1, int(round((axis_max - axis_min) / step_t)))
            t_dec    = 0 if step_t >= 1 else 1

            # --- Regenachse: gerundete Obergrenze, aber mind. 1 mm, damit
            #     Nieselregen klein dargestellt wird (statt voller Säulenhöhe). ---
            r_peak = max(rain)
            if r_peak <= 0:
                axis_max_r = 1.0
            else:
                step_r = _schoener_schritt(r_peak, 4)
                axis_max_r = max(math.ceil(r_peak / step_r) * step_r, 1.0)
            r_dec = 1 if axis_max_r < 5 else 0

            # --- Wert→Pixel-Hilfsfunktionen (Y-Achsen) ---
            def yt(v):
                return plot_bottom - (v - axis_min) / (axis_max - axis_min) * plot_h

            def yr(v):
                return plot_bottom - (v / axis_max_r) * plot_h

            def set_font(size, bold=False):
                f = QtGui.QFont("Arial", size)
                f.setBold(bold)
                painter.setFont(f)

            # --- Zeitstempel parsen (für Zeitachse, "Jetzt"-Linie, Tageslinien) ---
            try:
                tdts = [datetime.datetime.fromisoformat(s) for s in times]
            except (ValueError, TypeError):
                tdts = []

            # --- X-Achse: bevorzugt aus den echten Zeitstempeln (zeit-proportional).
            #     So werden herausgefilterte Lücken zeitlich korrekt auseinandergezogen,
            #     statt sie über den Listenindex zusammenzuschieben. Fällt auf die
            #     index-basierte Verteilung zurück, falls Zeitstempel fehlen/entartet. ---
            use_time_axis = len(tdts) == n and n >= 2 and tdts[-1] > tdts[0]
            if use_time_axis:
                _t0 = tdts[0]
                _total_s = (tdts[-1] - _t0).total_seconds()

                def x_at_time(t):
                    return margin_left + (t - _t0).total_seconds() / _total_s * plot_w

                xs = [x_at_time(td) for td in tdts]
            else:
                def x_at_time(_t):
                    return None

                xs = [margin_left + i * plot_w / max(n - 1, 1) for i in range(n)]

            # Pixel-X des aktuellen Zeitpunkts ermitteln. Mit Zeitachse direkt aus
            # der Zeit; sonst robust über Zeitstempel-Interpolation auf die xs.
            now_x = None
            if now_local is not None and use_time_axis and tdts[0] <= now_local <= tdts[-1]:
                now_x = x_at_time(now_local)
            elif now_local is not None and tdts and tdts[0] <= now_local <= tdts[-1]:
                for k in range(len(tdts) - 1):
                    if tdts[k] <= now_local <= tdts[k + 1]:
                        span = (tdts[k + 1] - tdts[k]).total_seconds() or 1.0
                        frac = (now_local - tdts[k]).total_seconds() / span
                        now_x = xs[k] + frac * (xs[k + 1] - xs[k])
                        break

            # --- Titel + Koordinaten-Untertitel ---
            set_font(17, bold=True)
            painter.setPen(QtGui.QPen(C_INK))
            painter.drawText(
                QtCore.QRect(0, 14, width, 26),
                QtCore.Qt.AlignCenter,
                tr("chart_title", lang)
            )
            set_font(10)
            painter.setPen(QtGui.QPen(C_MUTE))
            # Mit gefundenem Ort: "… – in der Nähe von <Ort>", sonst nur Koordinaten.
            if place:
                subtitle = tr("coords_near", lang,
                              lat=_fmt_num(lat, 4, lang),
                              lon=_fmt_num(lon, 4, lang), place=place)
            else:
                subtitle = tr("coords", lang,
                              lat=_fmt_num(lat, 4, lang), lon=_fmt_num(lon, 4, lang))
            painter.drawText(
                QtCore.QRect(0, 42, width, 18),
                QtCore.Qt.AlignCenter,
                subtitle,
            )

            # --- Vergangenheit dezent schattieren (links der "Jetzt"-Linie) ---
            if now_x is not None:
                painter.fillRect(
                    int(margin_left), int(margin_top),
                    int(now_x - margin_left), int(plot_h),
                    C_PAST
                )

            # --- Horizontale Gitterlinien + Y-Achsen-Beschriftung ---
            for i in range(n_ticks + 1):
                frac  = i / n_ticks
                y_pos = int(plot_bottom - frac * plot_h)

                painter.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.SolidLine))
                painter.drawLine(margin_left, y_pos, margin_left + plot_w, y_pos)

                # Beschriftung links (Temperatur) – Dezimaltrenner nach Sprache
                t_val = axis_min + i * step_t
                set_font(10)
                painter.setPen(QtGui.QPen(C_TEMP, 1))
                painter.drawText(
                    QtCore.QRect(0, y_pos - 9, margin_left - 8, 18),
                    QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                    f"{_fmt_num(t_val, t_dec, lang)}°"
                )

                # Beschriftung rechts (Regen)
                r_val = frac * axis_max_r
                painter.setPen(QtGui.QPen(C_RAIN_INK, 1))
                painter.drawText(
                    QtCore.QRect(margin_left + plot_w + 8, y_pos - 9, margin_right - 10, 18),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                    _fmt_num(r_val, r_dec, lang)
                )

            # --- Vertikale Tageslinien + Datums-Beschriftung ---
            set_font(10)
            if use_time_axis:
                # Eine Linie an jedem lokalen Mitternacht – zeitlich korrekt, auch
                # wenn einzelne Stunden herausgefiltert wurden.
                day = tdts[0].replace(hour=0, minute=0, second=0, microsecond=0)
                while day <= tdts[-1]:
                    if day >= tdts[0]:
                        x_pos = int(x_at_time(day))

                        painter.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.SolidLine))
                        painter.drawLine(x_pos, margin_top, x_pos, plot_bottom)

                        painter.setPen(QtGui.QPen(C_AXIS, 1))
                        painter.drawLine(x_pos, plot_bottom, x_pos, plot_bottom + 5)

                        painter.setPen(QtGui.QPen(C_MUTE, 1))
                        painter.drawText(
                            QtCore.QRect(x_pos - 48, plot_bottom + 7, 96, 16),
                            QtCore.Qt.AlignCenter,
                            _format_date_label(day, lang)
                        )
                    day += datetime.timedelta(days=1)
            else:
                # Rückfall ohne verlässliche Zeitstempel: alle 24 Indizes.
                for i in range(0, n, 24):
                    x_pos = int(xs[i])

                    painter.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.SolidLine))
                    painter.drawLine(x_pos, margin_top, x_pos, plot_bottom)

                    painter.setPen(QtGui.QPen(C_AXIS, 1))
                    painter.drawLine(x_pos, plot_bottom, x_pos, plot_bottom + 5)

                    if i < len(tdts):
                        date_lbl = _format_date_label(tdts[i], lang)
                    else:
                        d = times[i]
                        date_lbl = f"{d[8:10]}.{d[5:7]}."

                    painter.setPen(QtGui.QPen(C_MUTE, 1))
                    painter.drawText(
                        QtCore.QRect(x_pos - 48, plot_bottom + 7, 96, 16),
                        QtCore.Qt.AlignCenter,
                        date_lbl
                    )

            # --- Achsenlinien (links / rechts / unten), kein harter Vollrahmen ---
            painter.setPen(QtGui.QPen(C_AXIS, 1))
            painter.drawLine(margin_left, margin_top, margin_left, plot_bottom)
            painter.drawLine(margin_left + plot_w, margin_top, margin_left + plot_w, plot_bottom)
            painter.drawLine(margin_left, plot_bottom, margin_left + plot_w, plot_bottom)

            # --- Datenbereich beschneiden, damit Bézier-Überschwinger/Balken
            #     nicht über die Achsen hinausragen ---
            painter.save()
            painter.setClipRect(margin_left, margin_top, plot_w, plot_h)

            # --- Regenbalken (zuerst, damit die Temperaturlinie darüber liegt) ---
            bar_w = max(4, int(plot_w / n * 0.7))
            for i in range(n):
                if rain[i] > 0:
                    bx = int(xs[i]) - bar_w // 2
                    by = int(yr(rain[i]))
                    painter.fillRect(bx, by, bar_w, plot_bottom - by, C_RAIN)

            # --- Temperatur: geglättete Kurve (Catmull-Rom → kubische Bézier) ---
            pts = [QtCore.QPointF(xs[i], yt(temp[i])) for i in range(n)]

            def smooth_path(points):
                path = QtGui.QPainterPath()
                if not points:
                    return path
                path.moveTo(points[0])
                m = len(points)
                for j in range(m - 1):
                    p0 = points[j - 1] if j > 0 else points[j]
                    p1 = points[j]
                    p2 = points[j + 1]
                    p3 = points[j + 2] if j + 2 < m else points[j + 1]
                    # Catmull-Rom-Tangenten (Tension 1/6) als Bézier-Kontrollpunkte
                    c1 = QtCore.QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                                        p1.y() + (p2.y() - p0.y()) / 6.0)
                    c2 = QtCore.QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                                        p2.y() - (p3.y() - p1.y()) / 6.0)
                    path.cubicTo(c1, c2, p2)
                return path

            line_path = smooth_path(pts)

            # Flächenfüllung mit Verlauf (oben kräftig → unten transparent),
            # wirkt deutlich weniger "ausgewaschen" als eine flache Pastellfläche.
            fill_path = QtGui.QPainterPath()
            fill_path.moveTo(QtCore.QPointF(pts[0].x(), plot_bottom))
            fill_path.lineTo(pts[0])
            fill_path.connectPath(line_path)
            fill_path.lineTo(QtCore.QPointF(pts[-1].x(), plot_bottom))
            fill_path.closeSubpath()

            grad = QtGui.QLinearGradient(0.0, float(margin_top), 0.0, float(plot_bottom))
            grad.setColorAt(0.0, QtGui.QColor(228, 87, 46, 110))
            grad.setColorAt(1.0, QtGui.QColor(228, 87, 46, 0))
            painter.fillPath(fill_path, QtGui.QBrush(grad))

            pen_t = QtGui.QPen(C_TEMP, 2.6)
            pen_t.setCapStyle(QtCore.Qt.RoundCap)
            pen_t.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.strokePath(line_path, pen_t)

            # --- "Jetzt"-Linie (gestrichelt, innerhalb des Plots) ---
            if now_x is not None:
                nx = int(now_x)
                painter.setPen(QtGui.QPen(C_NOW, 1.4, QtCore.Qt.DashLine))
                painter.drawLine(nx, margin_top, nx, plot_bottom)

            painter.restore()  # Beschneidung wieder aufheben

            # --- "Jetzt"-Beschriftung oberhalb des Plots (außerhalb der Beschneidung) ---
            if now_x is not None:
                nx = int(now_x)
                set_font(9, bold=True)
                painter.setPen(QtGui.QPen(C_NOW))
                painter.drawText(
                    QtCore.QRect(nx - 30, margin_top - 16, 60, 14),
                    QtCore.Qt.AlignCenter,
                    tr("now", lang)
                )

            # --- Achsentitel (vertikal) ---
            set_font(12, bold=True)
            painter.setPen(QtGui.QPen(C_TEMP))
            painter.save()
            painter.translate(20, margin_top + plot_h / 2)
            painter.rotate(-90)
            painter.drawText(QtCore.QRect(-110, -16, 220, 30), QtCore.Qt.AlignCenter,
                             tr("temp_axis", lang))
            painter.restore()

            painter.setPen(QtGui.QPen(C_RAIN_INK))
            painter.save()
            painter.translate(width - 18, margin_top + plot_h / 2)
            painter.rotate(90)
            painter.drawText(QtCore.QRect(-110, -16, 220, 30), QtCore.Qt.AlignCenter,
                             tr("rain_axis", lang))
            painter.restore()

            # --- Legende (oben rechts, mit halbtransparentem Hintergrund) ---
            leg_w, leg_h = 250, 24
            leg_x = margin_left + plot_w - leg_w
            leg_y = margin_top + 8
            painter.fillRect(leg_x, leg_y, leg_w, leg_h, QtGui.QColor(255, 255, 255, 215))
            cy = leg_y + leg_h // 2

            set_font(10)
            # Temperatur
            painter.setPen(QtGui.QPen(C_TEMP, 3))
            painter.drawLine(leg_x + 10, cy, leg_x + 34, cy)
            painter.setPen(QtGui.QPen(C_INK))
            painter.drawText(leg_x + 40, cy + 4, tr("legend_temp", lang))
            # Regen
            rx = leg_x + 140
            painter.fillRect(rx, cy - 5, 24, 10, C_RAIN)
            painter.setPen(QtGui.QPen(C_INK))
            painter.drawText(rx + 30, cy + 4, tr("legend_rain", lang))
        finally:
            painter.end()

        # Geräte-Pixeldichte am Bild vermerken, damit es 1:1 (scharf) angezeigt wird.
        img.setDevicePixelRatio(scale)
        return img

    # -------------------------------------------------------------------------
    def canvasReleaseEvent(self, event) -> None:
        # Nur Linksklick auswerten – Rechts-/Mittelklick (Verschieben, Kontextmenü) ignorieren.
        if event.button() != QtCore.Qt.LeftButton:
            return

        lang = _current_lang()
        _log(tr("log_release", lang))

        p         = event.position() if hasattr(event, "position") else event.pos()
        map_point = self.canvas.getCoordinateTransform().toMapPoint(int(p.x()), int(p.y()))

        try:
            src = QgsProject.instance().crs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            if not src.isValid() or not dst.isValid():
                raise RuntimeError(tr("err_crs", lang))

            ct  = QgsCoordinateTransform(src, dst, QgsProject.instance())
            wgs = ct.transform(map_point)

            lat, lon = wgs.y(), wgs.x()
            # Datenschutz: ins Log nur gerundet (~1 km), nicht die exakte Position.
            _log(tr("log_coords", lang, lat=round(lat, 2), lon=round(lon, 2)))

            t, temp, rain, utc_offset = self.fetch_weather(lat, lon, lang)
            _log(tr("log_received", lang, n=len(temp)))

            # Optionale Anreicherung: nächstgelegenen Ortsnamen ermitteln (Nominatim).
            # Schlägt das fehl, bleibt place None und es werden nur die Koordinaten
            # gezeigt – kein Fehler im UI, da das Wetter die Hauptfunktion bleibt.
            place = self.reverse_geocode(lat, lon, lang)
            if place:
                _log(tr("log_place", lang, place=place))

            # Lokale "Jetzt"-Zeit am Ort = aktuelle UTC + utc_offset_seconds.
            now_local = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=utc_offset)
            ).replace(tzinfo=None)

            # In Geräteauflösung rendern → 1:1 anzeigen, kein unscharfer Downscale.
            try:
                dpr = float(self.iface.mainWindow().devicePixelRatioF())
            except Exception:
                dpr = 1.0

            img = self.create_png(
                lat, lon, t, temp, rain, now_local, scale=dpr, lang=lang, place=place
            )
            pixmap = QtGui.QPixmap.fromImage(img)
            pixmap.setDevicePixelRatio(dpr)

            dlg = QtWidgets.QDialog(self.iface.mainWindow())
            dlg.setWindowTitle("Weather Picker")

            layout = QtWidgets.QVBoxLayout()
            # Gleichmäßiger Rand rundum, kompakter Abstand zur Quellenzeile –
            # so bläht sich der Fußbereich nicht mehr auf.
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(10)

            # Bild anzeigen (ohne Skalierung → scharf)
            label_img = QtWidgets.QLabel()
            label_img.setPixmap(pixmap)
            label_img.setAlignment(QtCore.Qt.AlignCenter)
            layout.addWidget(label_img)

            # Quellen-/Lizenzhinweis: Wetterdaten immer (CC BY 4.0, Open-Meteo),
            # Ortsname nur, wenn er via Nominatim ermittelt wurde (© OSM-Mitwirkende,
            # ODbL – Attribution laut Nominatim-Nutzungsrichtlinie erforderlich).
            quellen = [tr("source", lang)]
            if place:
                quellen.append(tr("source_osm", lang))
            label_src = QtWidgets.QLabel("&nbsp;&nbsp;·&nbsp;&nbsp;".join(quellen))
            label_src.setOpenExternalLinks(True)
            label_src.setAlignment(QtCore.Qt.AlignCenter)
            layout.addWidget(label_src)

            dlg.setLayout(layout)
            # Dialog exakt auf den Inhalt zuschneiden (Bild + Quellenzeile + Ränder),
            # statt fester Größe → kein übergroßer grauer Fußbereich, Ränder bündig.
            dlg.adjustSize()

            _log(tr("log_dialog_open", lang))
            dlg.exec()
            _log(tr("log_dialog_closed", lang))

        except Exception as e:
            import traceback
            _log(traceback.format_exc(), level=Qgis.Critical)
            QtWidgets.QMessageBox.critical(self.iface.mainWindow(), "Weather Picker", str(e))
