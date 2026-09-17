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

from __future__ import annotations

import datetime
import os
import traceback

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsTask,
)
from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.PyQt import QtCore, QtGui, QtWidgets

from . import export, points_layer, settings
from .i18n import _current_lang, tr
from .api import fetch_weather, forward_geocode, reverse_geocode
from .chart import (gleitendes_tagesmittel, kumulierter_niederschlag,
                    zeichendaten)
from .processing_provider import WeatherPickerProvider
from .options import OptionsDialog
from .window import WeatherWindow


# Einheitlicher Log-Tag -> im QGIS-Log-Panel als eigener Reiter filterbar.
LOG_TAG = "Weather Picker"


def _log(message: object, level: int = Qgis.Info) -> None:
    """Status-/Debug-Ausgabe ins QGIS-Log-Panel statt auf stdout."""
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)


# =============================================================================
# Plugin-Klasse
# =============================================================================
def _jetzt_lokal(utc_offset: int) -> datetime.datetime:
    """Lokale "Jetzt"-Zeit am abgefragten Ort = aktuelle UTC + Versatz.

    Einmal an einer Stelle, weil Diagramm und Kartensymbol dieselbe Zeit
    brauchen - zweimal gerechnet liefen sie um die Laufzeit des Abrufs
    auseinander."""
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=utc_offset)).replace(tzinfo=None)


def _nach_karten_crs(lat: float, lon: float) -> QgsPointXY | None:
    """WGS84-Punkt ins CRS des Projekts umrechnen; ``None``, wenn das nicht geht.

    Gemeinsame Grundlage fuer das Schwenken der Karte und fuer den
    Klick-Marker – beide brauchen dieselbe Umrechnung, und zwei Kopien
    davon liefen frueher oder spaeter auseinander."""
    try:
        dst = QgsProject.instance().crs()
        src = QgsCoordinateReferenceSystem("EPSG:4326")
        if not src.isValid() or not dst.isValid():
            return None
        ct = QgsCoordinateTransform(src, dst, QgsProject.instance())
        return ct.transform(QgsPointXY(lon, lat))
    except Exception:
        _log(traceback.format_exc(), level=Qgis.Warning)
        return None


class WeatherPickerPlugin:
    """Plugin-Lebenszyklus: Toolbar/Action anlegen, Map-Tool umschalten, aufräumen."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.toolbar = None
        self._owns_toolbar = False  # nur selbst erzeugte Toolbar darf aufgeräumt werden
        self.action = None
        self.menu = None  # Ausklappmenü am Weather-Picker-Knopf
        self.actions = []
        self.tool = None  # erst in activate_tool() belegt; hält das aktive Map-Tool
        self.window = None  # wiederverwendetes Ergebnis-Fenster (lazy)

        # Marker auf dem zuletzt abgefragten Punkt. Ein einziger, der
        # verschoben wird – pro Klick einen neuen anzulegen haeuft Geister an,
        # die niemand mehr wegraeumt. Die WGS84-Koordinate wird mitgefuehrt,
        # damit sich der Marker nach einem CRS-Wechsel neu setzen laesst,
        # statt an der alten Bildschirmposition stehenzubleiben.
        self.marker = None
        self._marker_wgs84 = None
        self.canvas.destinationCrsChanged.connect(self._marker_neu_setzen)

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
        # Ein Knopf statt vier in der geteilten Toolbar: der vordere Teil
        # schaltet das Klick-Werkzeug, der Pfeil klappt Suche, Sammeln und
        # Einstellungen auf.
        self.action = QtWidgets.QAction(icon, "Weather Picker", self.iface.mainWindow())
        self.action.setToolTip(tr("action_tooltip", lang))
        self.action.setCheckable(True)
        self.action.triggered.connect(self.activate_tool)
        self.menu = QtWidgets.QMenu(self.iface.mainWindow())

        # --- Ortssuche (Lupe): Wetter für einen gesuchten Ort/Adresse ---
        search_action = QtWidgets.QAction(
            QtGui.QIcon(":/images/themes/default/search.svg"),
            tr("search_action", lang),
            self.iface.mainWindow(),
        )
        search_action.triggered.connect(self.search_place)
        self.menu.addAction(search_action)
        self.actions.append(search_action)

        # --- Punkte sammeln (Toggle): jedes Ergebnis in einen Layer schreiben ---
        # Aufnahme-Symbol, weil der Eintrag ein Umschalter ist: solange er
        # an ist, wird jedes Ergebnis mitgeschrieben.
        self.collect_action = QtWidgets.QAction(
            QtGui.QIcon(":/images/themes/default/mActionRecord.svg"),
            tr("collect_action", lang),
            self.iface.mainWindow(),
        )
        self.collect_action.setCheckable(True)
        self.collect_action.setChecked(settings.collect_points())
        self.collect_action.toggled.connect(self.toggle_collect)
        self.menu.addAction(self.collect_action)
        self.actions.append(self.collect_action)

        self.menu.addSeparator()

        # --- Einstellungen: Endpoint/Schlüssel/Einheiten ---
        settings_action = QtWidgets.QAction(
            QtGui.QIcon(":/images/themes/default/mActionOptions.svg"),
            tr("settings_action", lang),
            self.iface.mainWindow(),
        )
        settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(settings_action)
        self.actions.append(settings_action)

        # Das Menü muss an der Action hängen, BEVOR sie in die Toolbar kommt:
        # nur dann macht Qt 5 daraus einen Knopf mit sofort aufklappendem Pfeil
        # (MenuButtonPopup). Später angehängt bleibt es dort DelayedPopup, und
        # das Menü erscheint erst nach langem Drücken. Qt 6 schaltet auch
        # nachträglich um, die Reihenfolge schadet dort aber nicht.
        self.action.setMenu(self.menu)
        self.toolbar.addAction(self.action)
        self.actions.append(self.action)

        # Zuletzt und abgesichert: der Provider ist eine Zugabe, und ein
        # Fehler bei seiner Anmeldung darf nicht das Klick-Werkzeug
        # mitreissen, fuer das es das Plugin gibt.
        self.initProcessing()

    def initProcessing(self) -> None:
        """Nur den Processing-Teil hochfahren.

        QGIS ruft diese Methode selbst auf, wenn metadata.txt
        ``hasProcessingProvider=yes`` traegt - auch ohne Oberflaeche, etwa
        unter ``qgis_process`` oder im Modellbauer eines Server-Laufs.
        Ohne die Methode wuerde QGIS das Plugin in diesem Fall gar nicht
        laden (siehe qgis.utils.startProcessingPlugin).

        Mehrfach aufrufbar: QGIS ruft sie, und ``initGui`` ruft sie
        ebenfalls, damit der Provider auch dann steht, wenn die Flagge
        einmal nicht ausgewertet wird. Der zweite Aufruf findet den
        Provider bereits vor und tut nichts."""
        if getattr(self, "provider", None) is not None:
            return
        try:
            self.provider = WeatherPickerProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)
        except Exception as e:
            self.provider = None
            _log(f"Processing-Provider nicht angemeldet: {e}")

    def toggle_collect(self, checked: bool) -> None:
        settings.set_collect_points(checked)
        lang = _current_lang()
        self.iface.messageBar().pushMessage(
            "Weather Picker",
            tr("collect_on" if checked else "collect_off", lang),
            level=Qgis.Info,
        )

    def _ensure_tool(self) -> "WeatherPickerTool":
        """Map-Tool-Instanz holen oder einmalig anlegen. Wird sowohl vom Karten-
        Klick (als aktives Map-Tool) als auch von der Ortssuche genutzt; die Suche
        muss das Werkzeug dafür nicht als aktives Map-Tool setzen."""
        if self.tool is None:
            self.tool = WeatherPickerTool(self.iface, self.canvas, self.action, plugin=self)
        return self.tool

    def get_result_window(self) -> "WeatherWindow":
        """Ergebnis-Fenster holen oder einmalig anlegen (nicht-modal, wiederverwendet)."""
        if self.window is None:
            self.window = WeatherWindow(self.iface)
        return self.window

    def open_settings(self) -> None:
        lang = _current_lang()
        dlg = OptionsDialog(self.iface.mainWindow(), lang)
        if dlg.exec():
            self.iface.messageBar().pushMessage(
                "Weather Picker", tr("opt_saved", lang), level=Qgis.Success
            )

    def search_place(self) -> None:
        lang = _current_lang()
        text, ok = QtWidgets.QInputDialog.getText(
            self.iface.mainWindow(), tr("search_title", lang), tr("search_prompt", lang)
        )
        if not ok or not text.strip():
            return
        self._ensure_tool().search_and_show(text.strip())

    def activate_tool(self, checked: bool = False) -> None:
        # Umschaltbare Action: Bei erneutem Klick (checked=False) das Werkzeug
        # wieder abwählen, statt ein neues Map-Tool zu setzen.
        if not checked:
            if self.tool is not None and self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            return

        self.canvas.setMapTool(self._ensure_tool())

        self.iface.messageBar().pushMessage(
            "Weather Picker",
            tr("click_hint", _current_lang()),
            level=Qgis.Info
        )

    def setze_marker(self, lat: float, lon: float) -> None:
        """Den zuletzt abgefragten Punkt auf der Karte markieren."""
        punkt = _nach_karten_crs(lat, lon)
        if punkt is None:
            return
        if self.marker is None:
            self.marker = QgsVertexMarker(self.canvas)
            self.marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
            self.marker.setColor(QtGui.QColor("#7b1fa2"))      # wie die Jetzt-Linie
            self.marker.setFillColor(QtGui.QColor(123, 31, 162, 70))
            self.marker.setIconSize(14)
            self.marker.setPenWidth(3)
        self._marker_wgs84 = (lat, lon)
        self.marker.setCenter(punkt)
        self.marker.show()

    def _marker_neu_setzen(self) -> None:
        """Nach einem Wechsel des Projekt-CRS neu umrechnen – sonst bliebe der
        Marker an der alten Kartenposition liegen und zeigte auf den
        falschen Ort."""
        if self._marker_wgs84 is not None:
            self.setze_marker(*self._marker_wgs84)

    def _marker_entfernen(self) -> None:
        """Marker aus der Canvas-Szene nehmen.

        Ohne das bleibt nach jedem Plugin-Reload einer zurueck: ein
        QgsVertexMarker haengt an der Szene des Canvas, nicht am Plugin, und
        ueberlebt dessen Entladen. Beim Herunterfahren von QGIS kann der
        Canvas schon abgebaut sein, dann liefert scene() None."""
        if self.marker is not None and self.canvas.scene() is not None:
            self.canvas.scene().removeItem(self.marker)
        self.marker = None
        self._marker_wgs84 = None

    def unload(self) -> None:
        # Zuerst abmelden: ein Provider, der auf entladenen Plugin-Code
        # zeigt, laesst die Werkzeugkiste beim naechsten Oeffnen stolpern.
        if getattr(self, "provider", None) is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

        self._marker_entfernen()
        try:
            self.canvas.destinationCrsChanged.disconnect(self._marker_neu_setzen)
        except (TypeError, RuntimeError):
            pass   # nie verbunden oder Canvas bereits abgebaut

        # Aktives Map-Tool zurücksetzen, falls noch unseres aktiv ist –
        # sonst bleibt es als Zombie-Referenz im Canvas hängen. Einen laufenden
        # Abruf abbrechen (auch wenn das Tool nur per Ortssuche, nicht als aktives
        # Map-Tool existiert), damit nach dem Entladen kein Dialog mehr aufpoppt.
        if self.tool is not None:
            if self.tool._active_task is not None:
                self.tool._active_task.cancel()
            if self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            # Rück-Referenz aufs Plugin kappen: ein verspätet zurückkehrender Task
            # darf nach dem Entladen kein Fenster mehr neu anlegen (plugin is None →
            # _show_result steigt sauber aus).
            self.tool.plugin = None
        self.tool = None

        # Ergebnis-Fenster schließen und freigeben.
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            self.window = None

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
        if self.menu is not None:
            self.menu.deleteLater()
            self.menu = None

        # Haben wir die Toolbar selbst erzeugt und ist sie jetzt leer, geben wir
        # sie sauber frei (deleteLater statt removeToolBar, um die Verstecken-Falle
        # zu umgehen). Eine fremde/geteilte Toolbar bleibt unangetastet.
        if self._owns_toolbar and self.toolbar is not None and len(self.toolbar.actions()) == 0:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        self.toolbar = None
        self._owns_toolbar = False


# =============================================================================
# Hintergrund-Task: Wetterabruf + Geocoding ohne UI-Blockade
# =============================================================================
class WeatherFetchTask(QgsTask):
    """Holt Wetter- und Ortsdaten im Worker-Thread.

    ``run()`` läuft im Hintergrund-Thread (kein GUI-Zugriff erlaubt) und nutzt
    ausschließlich ``api.fetch_weather``/``api.reverse_geocode`` – beide gehen
    über ``QgsBlockingNetworkRequest``, das für Worker-Threads vorgesehen
    ist. ``finished()`` läuft wieder im Hauptthread und baut dort den Dialog.
    Dadurch friert QGIS während des bis zu 15 s langen Abrufs nicht mehr ein.

    Wird ``query`` gesetzt (Ortssuche), löst ``run()`` den Ort zuerst per
    ``api.forward_geocode`` in Koordinaten auf; bleibt das ergebnislos, endet der
    Task mit ``not_found=True`` (kein Fehler)."""

    def __init__(self, description: str, tool: "WeatherPickerTool",
                 lat, lon, lang: str, query: str | None = None) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.tool = tool          # Python-Ref hält das Tool über die Task-Laufzeit am Leben
        self.lat = lat            # bei Ortssuche zunächst None, in run() gesetzt
        self.lon = lon
        self.lang = lang
        self.query = query        # None = Karten-Klick, sonst Freitext-Ortssuche
        # Ergebnis-Felder, im Hauptthread aus finished() gelesen:
        self.result: tuple | None = None   # (times, temp, precip, utc_offset, extras)
        self.place: str | None = None
        self.not_found = False    # True, wenn die Ortssuche nichts fand
        self.error_msg: str | None = None

    def run(self) -> bool:
        """Im Worker-Thread: optional Ortssuche, dann Wetter, dann optional Ort."""
        try:
            if self.query is not None:
                hit = forward_geocode(self.query, self.lang)
                if hit is None:
                    self.not_found = True
                    return True   # kein Fehler – finished() zeigt nur einen Hinweis
                self.lat, self.lon, _label = hit
            times, temp, precip, utc_offset, extras = fetch_weather(
                self.lat, self.lon, self.lang
            )
        except Exception as exc:  # lokalisierte RuntimeError-Meldungen u. a.
            self.error_msg = str(exc)
            return False

        if self.isCanceled():
            return False

        # Geocoding ist optional und schlägt still auf None fehl (kein UI-Fehler).
        self.place = reverse_geocode(self.lat, self.lon, self.lang)
        self.result = (times, temp, precip, utc_offset, extras)
        return True

    def finished(self, ok: bool) -> None:
        """Im Hauptthread: Ergebnis an das Tool übergeben (Dialog/Fehlermeldung)."""
        self.tool._on_fetch_finished(self, ok)


class WeatherPickerTool(QgsMapTool):

    def __init__(self, iface, canvas, action: QtWidgets.QAction | None = None,
                 plugin=None) -> None:
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self.action = action
        self.plugin = plugin      # liefert das Ergebnis-Fenster (get_result_window)
        self._active_task = None  # höchstens ein laufender Abruf-Task gleichzeitig

    def deactivate(self) -> None:
        """Wird aufgerufen, wenn ein anderes Werkzeug aktiviert wird."""
        # Laufenden Abruf abbrechen → nach dem Werkzeugwechsel poppt kein Dialog
        # mehr auf (cancel() ⇒ isCanceled()==True ⇒ finished() blendet nichts ein).
        if self._active_task is not None:
            self._active_task.cancel()
        if self.action:
            self.action.setChecked(False)
        super().deactivate()

    # -------------------------------------------------------------------------
    def canvasReleaseEvent(self, event) -> None:
        # Nur Linksklick auswerten – Rechts-/Mittelklick (Verschieben, Kontextmenü) ignorieren.
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return

        lang = _current_lang()
        _log(tr("log_release", lang))

        p         = event.position() if hasattr(event, "position") else event.pos()
        # toMapCoordinatesF statt toMapPoint: letzteres ist veraltet und
        # meldet das bei jedem Klick ins Protokoll. Die
        # F-Fassung nimmt ausserdem Fliesskomma-Pixel, wie sie
        # event.position() liefert - das int() davor warf den
        # Subpixel-Anteil weg.
        map_point = self.canvas.getCoordinateTransform().toMapCoordinatesF(
            p.x(), p.y())

        # CRS-Transformation läuft synchron (schnell, kein Netz). Der eigentliche
        # Abruf wandert danach in einen Hintergrund-Task, damit die GUI frei bleibt.
        try:
            src = QgsProject.instance().crs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            if not src.isValid() or not dst.isValid():
                raise RuntimeError(tr("err_crs", lang))
            ct  = QgsCoordinateTransform(src, dst, QgsProject.instance())
            wgs = ct.transform(map_point)
        except Exception as e:
            _log(traceback.format_exc(), level=Qgis.Critical)
            QtWidgets.QMessageBox.critical(self.iface.mainWindow(), "Weather Picker", str(e))
            return

        lat, lon = wgs.y(), wgs.x()
        # Datenschutz: ins Log nur gerundet (~1 km), nicht die exakte Position.
        _log(tr("log_coords", lang, lat=round(lat, 2), lon=round(lon, 2)))

        self._start_fetch(lat, lon, lang)

    # -------------------------------------------------------------------------
    def search_and_show(self, query: str) -> None:
        """Wetter für einen per Freitext gesuchten Ort/Adresse anzeigen.
        Die Ortsauflösung läuft im selben Hintergrund-Task wie der Abruf."""
        self._start_fetch(None, None, _current_lang(), query=query)

    def _start_fetch(self, lat, lon, lang: str, query: str | None = None) -> None:
        """Abruf als Hintergrund-Task starten (nicht-blockierend). Mit ``query``
        wird der Ort zuerst gesucht (Forward-Geocoding), sonst zählen lat/lon."""
        # Höchstens ein Abruf gleichzeitig: schnelles Mehrfachklicken soll keine
        # Task-Lawine auslösen. Ein bereits laufender Task bleibt unberührt.
        if self._active_task is not None:
            return

        task = WeatherFetchTask(tr("chart_title", lang), self, lat, lon, lang, query=query)
        self._active_task = task
        self.iface.messageBar().pushMessage(
            "Weather Picker", tr("loading", lang), level=Qgis.Info
        )
        _log(tr("log_search", lang, q=query) if query is not None
             else tr("log_task_start", lang))
        QgsApplication.taskManager().addTask(task)

    def _pan_to(self, lat: float, lon: float) -> None:
        """Karten-Canvas auf den (WGS84-)Punkt zentrieren – visuelles Feedback nach
        einer Ortssuche. Schlägt die Transformation fehl, passiert nichts."""
        punkt = _nach_karten_crs(lat, lon)
        if punkt is None:
            return
        self.canvas.setCenter(punkt)
        self.canvas.refresh()

    def _on_fetch_finished(self, task: "WeatherFetchTask", ok: bool) -> None:
        """Callback im Hauptthread, sobald der Task fertig oder abgebrochen ist."""
        # Nur den eigenen aktiven Task-Slot leeren – ein veralteter Task, der nach
        # einem Tool-Wechsel noch zurückkommt, soll einen neuen Lauf nicht stören.
        if self._active_task is task:
            self._active_task = None

        if task.isCanceled():
            return

        # Ortssuche ohne Treffer: nur ein dezenter Hinweis, kein Fehlerdialog.
        if task.not_found:
            self.iface.messageBar().pushMessage(
                "Weather Picker", tr("search_none", task.lang, q=task.query),
                level=Qgis.Info
            )
            return

        if not ok or task.result is None:
            msg = task.error_msg or tr("err_nodata", task.lang)
            _log(msg, level=Qgis.Critical)
            QtWidgets.QMessageBox.critical(self.iface.mainWindow(), "Weather Picker", msg)
            return

        times, temp, precip, utc_offset, extras = task.result
        _log(tr("log_received", task.lang, n=len(temp)))
        if task.place:
            _log(tr("log_place", task.lang, place=task.place))

        # Bei einer Ortssuche die Karte auf den gefundenen Punkt schwenken.
        if task.query is not None:
            self._pan_to(task.lat, task.lon)

        # Punkt auf der Karte markieren – erst hier, nach den Abbruch- und
        # Fehler-Gates: ein fehlgeschlagener Abruf soll den Marker nicht auf
        # eine Stelle setzen, zu der es gar kein Ergebnis gibt. Beim Wechsel
        # des Werkzeugs bleibt er absichtlich stehen, denn das Ergebnis-Fenster
        # bleibt ebenfalls offen und der Marker sagt, wozu es gehoert.
        if self.plugin is not None:
            self.plugin.setze_marker(task.lat, task.lon)

        # Optional: Ergebnis in den Sammel-Layer schreiben. Ein Fehler hier darf
        # die Diagramm-Anzeige nie verhindern → eigener try/except.
        if settings.collect_points():
            try:
                points_layer.add_point(task.lat, task.lon, task.place, extras,
                                       task.lang, tr("points_layer", task.lang),
                                       diagramm=self._diagramm_fuer_karte(
                                           task, times, temp, precip,
                                           utc_offset, extras))
                _log(tr("log_point_added", task.lang))
            except Exception:
                _log(traceback.format_exc(), level=Qgis.Warning)

        self._show_result(task.lat, task.lon, times, temp, precip,
                          utc_offset, task.place, task.lang, extras)

    # -------------------------------------------------------------------------
    def _diagramm_fuer_karte(self, task, times, temp, precip, utc_offset,
                             extras) -> str | None:
        """Diagramm fuer das Kartensymbol ablegen; None, wenn abgeschaltet.

        Ein Fehler darf weder den Sammel-Layer noch die Anzeige verhindern -
        im schlechtesten Fall traegt der Punkt eben kein Bild."""
        if not settings.flag("chart_marker"):
            return None
        try:
            daten = zeichendaten(
                task.lat, task.lon, times, temp, precip,
                _jetzt_lokal(utc_offset), lang=task.lang, place=task.place,
                extras=extras, units=settings.unit_symbols())
            return export.diagramm_ablegen(points_layer.bildordner(), daten,
                                           task.lat, task.lon)
        except Exception:
            _log(traceback.format_exc(), level=Qgis.Warning)
            return None

    def _show_result(self, lat, lon, times, temp, precip, utc_offset, place, lang,
                     extras=None) -> None:
        """Diagramm rendern und im wiederverwendeten Ergebnis-Fenster anzeigen.

        Ein einziges nicht-modales Fenster wird aktualisiert (kein modaler Dialog
        pro Klick), sodass man zügig über die Karte klicken kann. Den Export
        (PNG/Zwischenablage) übernimmt das Fenster mit dem zuletzt gezeigten Bild."""
        try:
            # Lokale "Jetzt"-Zeit am Ort = aktuelle UTC + utc_offset_seconds.
            now_local = _jetzt_lokal(utc_offset)

            # In Geräteauflösung rendern → 1:1 anzeigen, kein unscharfer Downscale.
            try:
                dpr = float(self.iface.mainWindow().devicePixelRatioF())
            except Exception:
                dpr = 1.0

            if self.plugin is None:
                return  # ohne Plugin-Kontext (z. B. isolierter Test) kein Fenster

            # Gleitendes Mittel und Startindex der kumulierten Summe hier auf
            # der vollen Reihe berechnen, nicht im Diagramm: beim Zoomen
            # schneidet das Fenster die Reihe, und auf dem Ausschnitt
            # gerechnet verloere das Mittel an jedem Rand zwoelf Stunden,
            # waehrend die Summe "ab jetzt" am Ausschnittsanfang begaenne.
            tdts = []
            try:
                tdts = [datetime.datetime.fromisoformat(s) for s in times]
            except (ValueError, TypeError):
                pass
            zeitachse = (len(tdts) == len(temp) and len(temp) >= 2
                         and tdts[-1] > tdts[0])
            temp_mittel = (gleitendes_tagesmittel(tdts, temp)
                           if zeitachse else [None] * len(temp))
            kum_ab = next((i for i, td in enumerate(tdts) if td >= now_local),
                          len(temp)) if zeitachse else 0
            kum_werte = kumulierter_niederschlag(precip, kum_ab)

            daten = {
                "times": times, "temp": temp, "precip": precip,
                "now_local": now_local, "place": place, "extras": extras,
                "units": settings.unit_symbols(),
                "temp_mittel": temp_mittel, "kum_werte": kum_werte,
                # Die geparste Zeitreihe mitgeben, nicht nur die Textform:
                # das Fenster rendert beim Zoomen bei jedem Rad-Schritt neu
                # und muesste sie sonst jedesmal aus rund 216 Zeichenketten
                # neu aufbauen.
                "tdts": tdts,
            }
            self.plugin.get_result_window().update_result(
                daten, dpr, lat, lon, place, lang)
            _log(tr("log_dialog_open", lang))

        except Exception as e:
            _log(traceback.format_exc(), level=Qgis.Critical)
            QtWidgets.QMessageBox.critical(self.iface.mainWindow(), "Weather Picker", str(e))
