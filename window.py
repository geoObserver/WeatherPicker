"""Weather Picker - wiederverwendetes Ergebnis-Fenster (nicht-modal).

Ein einziges, nicht-modales QDialog wird bei jedem Ergebnis aktualisiert und nach
vorn geholt; so kann der Anwender zuegig ueber die Karte klicken, ohne pro Klick
ein neues modales Fenster schliessen zu muessen. Das Fenster haelt das zuletzt
gerenderte QImage fuer den Export (PNG speichern / in die Zwischenablage).
"""
from __future__ import annotations

import datetime
import os

from qgis.core import Qgis, QgsProject, QgsRasterLayer
from qgis.PyQt import QtCore, QtGui, QtWidgets

from .chart import (LOGICAL_W, MARGIN_LEFT, PLOT_W, render_chart,
                    sichtfenster_indizes, zeichendaten)
from .i18n import tr
from . import export, georef, settings

# Mindestspanne der Ansicht; deckt sich mit sichtfenster_indizes.
_MIN_SPANNE_H = 6.0


def _maus_x(event) -> int:
    """x-Position eines Maus- oder Radereignisses, Qt5 wie Qt6.

    Gemessen gegen Qt 5.15.13 und Qt 6.11.0: die beiden Ereignistypen haben
    ihre Methoden gegenlaeufig verloren. QMouseEvent kennt unter Qt5 nur
    pos() und bekommt position() erst mit Qt6; QWheelEvent hat position()
    schon unter Qt5 und verliert pos() mit Qt6. Es gibt also keine einzelne
    Methode, die beide Typen auf beiden Versionen tragen - deshalb hier
    abfragen statt auf eine festzulegen. Genau das war der Fehler: mit
    position() fuer beides lief das Zoomen unter Qt5 und verdeckte, dass
    jedes Ziehen mit AttributeError abstuerzte."""
    if hasattr(event, "position"):
        return int(event.position().x())
    return int(event.pos().x())


class WeatherWindow(QtWidgets.QDialog):
    """Nicht-modales Fenster mit Diagramm, Quellen-/Lizenzzeile und Export-Buttons."""

    def __init__(self, iface) -> None:
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Weather Picker")
        self.setModal(False)        # nicht-modal: blockiert QGIS nicht
        self._img = None            # zuletzt gerendertes QImage (fuer Export)
        self._lat = 0.0
        self._lon = 0.0
        self._lang = "de"

        # Rohdaten des letzten Ergebnisses. Das Fenster rendert selbst nach,
        # damit ein Zoom nicht bei jedem Rad-Schritt einen neuen Netz-Abruf
        # braucht - die Daten liegen bereits vollstaendig vor.
        self._daten = None
        self._dpr = 1.0
        self._view = None           # (start, ende) oder None fuer Gesamtansicht
        self._ziehen_ab = None      # Mausposition beim Beginn des Verschiebens
        self._render_offen = False  # Neu-Rendern ist bereits eingeplant

        self.label_img = QtWidgets.QLabel()
        self.label_img.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.label_src = QtWidgets.QLabel()
        self.label_src.setOpenExternalLinks(True)
        self.label_src.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.btn_reset = QtWidgets.QPushButton()
        self.btn_reset.clicked.connect(self.ansicht_zuruecksetzen)
        self.btn_reset.setEnabled(False)   # erst sinnvoll, wenn gezoomt wurde
        self.btn_copy = QtWidgets.QPushButton()
        self.btn_copy.clicked.connect(self._copy_clipboard)

        # Aufklappbare Schaltflaeche statt eines einfachen Knopfes: der
        # vordere Teil oeffnet den Dialog mit allen Formaten, der Pfeil
        # bietet sie einzeln an. Wer weiss, was er will, spart damit den
        # Umweg ueber die Filterliste im Dateidialog.
        self.btn_save = QtWidgets.QToolButton()
        self.btn_save.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_save.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        # Ueber das Lambda, nicht direkt: clicked liefert den
        # Gedrueckt-Zustand als erstes Argument mit, und PyQt reicht ihn an
        # jeden Slot durch, der ein Argument annehmen kann. Direkt
        # verbunden kam so False als Formatangabe an.
        self.btn_save.clicked.connect(lambda _geprueft=False: self._save_png())
        self._menu_save = QtWidgets.QMenu(self.btn_save)
        self.btn_save.setMenu(self._menu_save)
        # Eintraege einmal anlegen und spaeter nur beschriften: neu aufbauen
        # muesste bei jedem Ergebnis geschehen, obwohl sich nur die Sprache
        # aendern kann.
        self._aktion_je_format = {}
        for endung, _filter in export.formate("en"):
            aktion = self._menu_save.addAction("")
            aktion.triggered.connect(
                lambda _geprueft=False, e=endung: self._save_png(e))
            self._aktion_je_format[endung] = aktion
        # Eigener Eintrag statt eines Hakens im Dateidialog: der statische
        # QFileDialog nimmt keine zusaetzlichen Bedienelemente auf.
        self._aktion_tif_laden = self._menu_save.addAction("")
        self._aktion_tif_laden.triggered.connect(
            lambda _geprueft=False: self._save_png("tif", laden=True))

        # Die beiden abgeleiteten Serien liegen ueber den Messwerten und sind
        # nicht fuer jede Frage hilfreich. Als Haken direkt am Bild, nicht im
        # Optionsdialog: die Entscheidung faellt beim Ansehen des Diagramms.
        # Der Zustand wird in den Einstellungen gehalten und gilt auch fuer
        # das naechste Ergebnis.
        self.chk_mittel = QtWidgets.QCheckBox()
        self.chk_kumuliert = QtWidgets.QCheckBox()
        self.chk_mittel.setChecked(settings.flag("show_mean"))
        self.chk_kumuliert.setChecked(settings.flag("show_cumulative"))
        self.chk_mittel.toggled.connect(
            lambda an: self._serie_umschalten("show_mean", an))
        self.chk_kumuliert.toggled.connect(
            lambda an: self._serie_umschalten("show_cumulative", an))

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addWidget(self.btn_reset)
        btn_row.addSpacing(16)
        btn_row.addWidget(self.chk_mittel)
        btn_row.addWidget(self.chk_kumuliert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_copy)
        btn_row.addWidget(self.btn_save)

        # Rad und Ziehen landen nicht von selbst bei einem QLabel - es nimmt
        # keine Mausereignisse entgegen. Ein Ereignisfilter holt sie ab.
        self.label_img.installEventFilter(self)
        self.label_img.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.label_img)
        layout.addWidget(self.label_src)
        layout.addLayout(btn_row)
        self.setLayout(layout)

        self._beschriften(self._lang)

    def update_result(self, daten: dict, dpr: float, lat: float, lon: float,
                      place: str | None, lang: str) -> None:
        """Fenster mit einem neuen Ergebnis fuellen und sichtbar nach vorn holen.

        ``daten`` traegt die Rohreihen statt eines fertigen Bildes, damit das
        Fenster beim Zoomen selbst nachrendern kann, ohne erneut abzurufen."""
        self._daten = daten
        self._dpr = dpr
        self._lat = lat
        self._lon = lon
        self._lang = lang
        self._view = None       # neues Ergebnis startet immer in der Gesamtsicht
        self._zeichne()

        quellen = [tr("source", lang)]
        if place:
            quellen.append(tr("source_osm", lang))
        self.label_src.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(quellen))
        self._beschriften(lang)

        self.adjustSize()        # Fenster auf den Inhalt zuschneiden
        self.show()              # nicht-modal (kein exec → kein Block, kein Stau)
        self.raise_()
        self.activateWindow()

    def _beschriften(self, lang: str) -> None:
        """Alle Bedienelemente in der gewuenschten Sprache beschriften.

        Schon beim Aufbau aufgerufen, nicht erst beim ersten Ergebnis: sonst
        stuenden Knoepfe und Menueeintraege leer da, falls das Fenster je vor
        dem ersten Abruf sichtbar wird.

        Die Haken bekommen eine ausgeschriebene Beschriftung statt der
        Legendentexte: in der Legende steht die Marke daneben und macht klar,
        welche Linie gemeint ist. Am Haken fehlt dieser Bezug - "kumuliert ab
        jetzt" allein sagt nicht, was kumuliert wird."""
        self.btn_reset.setText(tr("btn_reset", lang))
        for endung, aktion in self._aktion_je_format.items():
            aktion.setText(tr("save_as", lang, f=export.anzeigename(endung)))
        self._aktion_tif_laden.setText(tr("save_as_tif_load", lang))
        self.chk_mittel.setText(tr("chk_mean", lang))
        self.chk_kumuliert.setText(tr("chk_cumulative", lang))
        self.btn_copy.setText(tr("btn_copy", lang))
        self.btn_save.setText(tr("btn_save", lang))

    # -------------------------------------------------------------------------
    def _zeichne(self) -> None:
        """Diagramm aus den gehaltenen Rohdaten mit dem aktuellen Sichtfenster
        rendern und anzeigen."""
        d = self._daten
        if not d:
            return
        self._img = render_chart(
            self._lat, self._lon, d["times"], d["temp"], d["precip"],
            d["now_local"], scale=self._dpr, lang=self._lang,
            place=d["place"], extras=d["extras"], units=d["units"],
            view=self._view, temp_mittel=d["temp_mittel"],
            kum_werte=d["kum_werte"],
            mittel_an=self.chk_mittel.isChecked(),
            kumuliert_an=self.chk_kumuliert.isChecked(),
            tdts=d.get("tdts"),
        )
        pixmap = QtGui.QPixmap.fromImage(self._img)
        pixmap.setDevicePixelRatio(self._dpr)
        self.label_img.setPixmap(pixmap)

    def _serie_umschalten(self, schluessel: str, an: bool) -> None:
        """Eine abgeleitete Serie ein- oder ausblenden und die Wahl merken.

        Neu gerendert wird aus den bereits gehaltenen Rohdaten - kein
        Netz-Abruf, die Reihen liegen vollstaendig vor."""
        settings.set_flag(schluessel, an)
        self._zeichne()

    def _zeichne_gebuendelt(self) -> None:
        """Neu-Rendern auf den naechsten Ereignisdurchlauf verschieben.

        Ein voller Render liegt bei doppelter Geraetepixeldichte bei
        2560x1412 Pixeln mit Kantenglaettung - bei 60 Rad- oder
        Ziehereignissen pro Sekunde ruckelt das. Mehrere Ereignisse
        innerhalb eines Durchlaufs fallen so zu einem Render zusammen."""
        if self._render_offen:
            return
        self._render_offen = True

        def lauf():
            self._render_offen = False
            try:
                self._zeichne()
            except RuntimeError:
                # Das Fenster wurde zwischen Planung und Ausfuehrung
                # zerstoert (Plugin-Reload mitten in einer Zieh-Geste). Der
                # Zugriff auf das geloeschte QLabel wirft dann
                # "wrapped C/C++ object ... has been deleted" - hier ist
                # nichts mehr zu zeichnen, also still aussteigen.
                pass

        QtCore.QTimer.singleShot(16, lauf)

    # --- Zoom und Verschieben ------------------------------------------------
    def _zeit_bei(self, x_im_label: int):
        """Bildschirm-x im Label in die zugehoerige Zeit umrechnen.

        Das Pixmap sitzt zentriert im Label, deshalb muss der seitliche
        Leerraum abgezogen werden, bevor die Plot-Geometrie greift."""
        d = self._daten
        if not d:
            return None
        start, ende = self._sichtbereich()
        if start is None:
            return None
        rand = max(0, (self.label_img.width() - LOGICAL_W) // 2)
        anteil = (x_im_label - rand - MARGIN_LEFT) / PLOT_W
        anteil = min(1.0, max(0.0, anteil))
        return start + (ende - start) * anteil

    def _gesamtbereich(self):
        """Zeitbereich der vollstaendigen Reihe."""
        d = self._daten
        if not d or not d["times"]:
            return None, None
        try:
            return (datetime.datetime.fromisoformat(d["times"][0]),
                    datetime.datetime.fromisoformat(d["times"][-1]))
        except (ValueError, TypeError):
            return None, None

    def _sichtbereich(self):
        """Aktuell gezeigter Zeitbereich – bei Gesamtsicht die ganze Reihe."""
        return self._view if self._view is not None else self._gesamtbereich()

    def ansicht_zuruecksetzen(self) -> None:
        """Zurueck zur Gesamtansicht."""
        if self._view is not None:
            self._view = None
            self.btn_reset.setEnabled(False)
            self._zeichne()

    def _zoomen(self, faktor: float, anker) -> None:
        """Um ``anker`` herum zoomen; der Zeitpunkt unter dem Zeiger bleibt
        stehen, damit das Rad dorthin fuehrt, wohin man zeigt."""
        start, ende = self._sichtbereich()
        if start is None or anker is None:
            return
        links = (anker - start) * faktor
        rechts = (ende - anker) * faktor
        neu_start, neu_ende = anker - links, anker + rechts

        # Die gespeicherte Spanne selbst klemmen, nicht nur die gerenderte.
        # Ohne das schrumpft _view unbegrenzt weiter, waehrend das Bild bei
        # der Mindestspanne stehen bleibt - man muesste dann ebenso viele
        # Schritte zurueckdrehen, bis sich ueberhaupt wieder etwas bewegt.
        spanne_h = (neu_ende - neu_start).total_seconds() / 3600.0
        if spanne_h < _MIN_SPANNE_H:
            mitte = neu_start + (neu_ende - neu_start) / 2
            halb = datetime.timedelta(hours=_MIN_SPANNE_H / 2)
            neu_start, neu_ende = mitte - halb, mitte + halb

        # Nach aussen ist die volle Reihe die Grenze: weiter herausgedreht
        # entsteht nur Leerraum an den Seiten.
        voll_start, voll_ende = self._gesamtbereich()
        if voll_start is not None and neu_start <= voll_start and neu_ende >= voll_ende:
            self.ansicht_zuruecksetzen()
            return

        self._view = (neu_start, neu_ende)
        self.btn_reset.setEnabled(True)
        self._zeichne_gebuendelt()

    def _verschieben(self, dx_pixel: int) -> None:
        start, ende = self._sichtbereich()
        if start is None or self._view is None:
            return
        versatz = (ende - start) * (-dx_pixel / PLOT_W)
        neu_start, neu_ende = start + versatz, ende + versatz

        # An den Enden der Reihe anschlagen statt ins Leere zu schieben.
        voll_start, voll_ende = self._gesamtbereich()
        if voll_start is not None:
            if neu_start < voll_start:
                neu_ende += voll_start - neu_start
                neu_start = voll_start
            if neu_ende > voll_ende:
                neu_start -= neu_ende - voll_ende
                neu_ende = voll_ende
        self._view = (neu_start, neu_ende)
        self._zeichne_gebuendelt()

    def eventFilter(self, obj, event):
        if obj is not self.label_img or not self._daten:
            return super().eventFilter(obj, event)
        typ = event.type()
        E = QtCore.QEvent.Type

        if typ == E.Wheel:
            # Position ueber _maus_x: Qt5 und Qt6 tragen sie verschieden.
            stufen = event.angleDelta().y()
            if stufen:
                self._zoomen(1 / 1.25 if stufen > 0 else 1.25,
                             self._zeit_bei(_maus_x(event)))
            return True

        if typ == E.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._ziehen_ab = _maus_x(event)
            self.label_img.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            return True

        if typ == E.MouseMove and self._ziehen_ab is not None:
            x = _maus_x(event)
            self._verschieben(x - self._ziehen_ab)
            self._ziehen_ab = x
            return True

        if typ == E.MouseButtonRelease:
            self._ziehen_ab = None
            self.label_img.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            return True

        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key.Key_Escape and self._view is not None:
            self.ansicht_zuruecksetzen()   # Escape schliesst sonst das Fenster
            return
        super().keyPressEvent(event)

    def _zeichendaten(self) -> dict | None:
        """Die aufbereiteten Reihen fuer den aktuellen Ausschnitt.

        Dieselbe Aufbereitung wie beim Anzeigen, damit ein Export genau das
        liefert, was im Fenster steht - und nicht heimlich die volle Reihe."""
        d = self._daten
        if not d:
            return None
        return zeichendaten(
            self._lat, self._lon, d["times"], d["temp"], d["precip"],
            d["now_local"], lang=self._lang, place=d["place"],
            extras=d["extras"], units=d["units"], view=self._view,
            temp_mittel=d["temp_mittel"], kum_werte=d["kum_werte"],
            mittel_an=self.chk_mittel.isChecked(),
            kumuliert_an=self.chk_kumuliert.isChecked(),
            tdts=d.get("tdts"),
        )

    def _dateiname(self, endung: str) -> str:
        """Vorschlag fuer den Dateinamen."""
        return export.dateiname(self._lat, self._lon,
                                export.stempel_aus(self._daten),
                                endung=endung, view=self._view)

    def _geo_angaben(self) -> dict | None:
        """Ziel-CRS, Bodenbreite und Ankerpunkt fuer ein GeoTIFF.

        Die Breite folgt dem aktuellen Kartenausschnitt: so ist das Bild
        hinterher so gross, wie es beim Export ausgesehen hat. Nach einer
        Zahl in Metern zu fragen hiesse, eine Groesse zu erfragen, die
        vorher niemand kennt."""
        karten_crs, ausschnitt = None, None
        try:
            canvas = self.iface.mapCanvas()
            if canvas is not None:
                karten_crs = canvas.mapSettings().destinationCrs()
                ausschnitt = canvas.extent()
        except (AttributeError, RuntimeError):
            # ohne Karte (Skript, Test) greift der Rueckfall in georef
            karten_crs, ausschnitt = None, None
        return georef.geo_angaben(karten_crs, ausschnitt, self._lon, self._lat)

    def _layer_auf_pfad_entfernen(self, pfad: str) -> None:
        """Projekt-Layer loesen, die auf diese Datei zeigen.

        Windows gibt eine Datei nicht frei, solange ein Layer sie offen
        haelt - ein zweiter Export in dieselbe Datei liefe sonst in einen
        Sperrfehler."""
        ziel = os.path.normcase(os.path.abspath(pfad))
        treffer = [
            kennung for kennung, layer
            in QgsProject.instance().mapLayers().items()
            if os.path.normcase(os.path.abspath(
                layer.source().split("|", 1)[0])) == ziel
        ]
        if treffer:
            QgsProject.instance().removeMapLayers(treffer)

    def _geotiff_schreiben(self, pfad: str, daten: dict,
                           geo: dict | None) -> None:
        """GeoTIFF erst daneben schreiben, dann an seinen Platz ruecken.

        Direkt auf den Zielpfad geht nicht: haelt ein geladener Layer die
        Datei offen, scheitert das Schreiben unter Windows an der Sperre.
        Den Layer vorher zu loesen war aber auch falsch - schlug das
        Schreiben danach fehl (volle Platte, fehlendes GDAL), war der
        Layer weg, ohne dass etwas an seine Stelle getreten waere.

        Also in dieser Reihenfolge: schreiben, loesen, umbenennen. Bis
        zum letzten Schritt bleibt der bisherige Stand unangetastet."""
        vorlaeufig = export.vorlaeufiger_pfad(pfad)
        try:
            export.speichern(vorlaeufig, daten, self._dpr, geo)
            self._layer_auf_pfad_entfernen(pfad)
            os.replace(vorlaeufig, pfad)
        finally:
            # Nach os.replace ist da nichts mehr; nur ein abgebrochener
            # Lauf laesst die Zwischendatei liegen.
            if os.path.exists(vorlaeufig):
                try:
                    os.remove(vorlaeufig)
                except OSError:
                    pass

    def _in_karte_laden(self, pfad: str) -> None:
        """Das geschriebene GeoTIFF als Rasterlayer ins Projekt haengen."""
        name = export.layer_name(self._lat, self._lon,
                                 export.stempel_aus(self._daten),
                                 ort=(self._daten or {}).get("place"),
                                 view=self._view)
        layer = QgsRasterLayer(pfad, name)
        if not layer.isValid():
            raise OSError(pfad)
        QgsProject.instance().addMapLayer(layer)

    def _save_png(self, format_endung: str | None = None,
                  laden: bool = False) -> None:
        """Speichern-Dialog. Ohne Angabe mit allen Formaten zur Auswahl,
        sonst direkt auf das gewaehlte festgelegt.

        Das Format folgt dem Dateifilter, nicht einer eigenen Auswahlliste:
        so steht die Entscheidung an der Stelle, an der der Anwender ohnehin
        den Namen vergibt, und eine getippte Endung gewinnt gegen den
        Filter. Genau deshalb entscheidet ueber Georeferenz und Laden die
        tatsaechliche Endung und nicht der angeklickte Menueeintrag: wer im
        GeoTIFF-Eintrag eine .png tippt, bekommt ein PNG und keinen
        Rasterlayer."""
        if not isinstance(format_endung, (str, type(None))):
            # Zweite Absicherung gegen ein durchgereichtes Signal-Argument.
            # Ein falsches Format waere hier kein Schoenheitsfehler: die
            # Ausnahme entstuende in einem Qt-Slot, und die beendet unter
            # PyQt den ganzen Prozess.
            format_endung = None
        daten = self._zeichendaten()
        if daten is None:
            return
        filter_endung = export.filter_zu_endung(self._lang)
        if format_endung is None:
            vorgabe = "png"
            filter_text = ";;".join(f for _e, f in export.formate(self._lang))
        else:
            vorgabe = format_endung
            filter_text = export.endung_zu_filter(self._lang)[format_endung]
        projekt = QgsProject.instance().fileName()
        if laden and not projekt:
            # Ungespeichertes Projekt: ohne Rueckfrage in den Temp-Ordner des
            # Systems, weil es noch keinen Ort gibt, der zum Projekt gehoert.
            # Bewusst so entschieden (Anwender, 2026-09-14), obwohl QGIS einen
            # Layer aus dem Temp-Ordner fuer temporaer haelt (isTemporary) und
            # ihn beim Leeren des Projekts auf einem eigenen Weg abbaut, der
            # unter Windows abstuerzen kann. Die Diagramme der Sammelpunkte
            # meiden den Temp-Ordner deshalb (points_layer.bildordner).
            pfad = os.path.join(
                QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.TempLocation),
                self._dateiname(vorgabe))
            gewaehlt = ""
        else:
            vorschlag = self._dateiname(vorgabe)
            if laden:
                # Gespeichertes Projekt: der Dialog oeffnet im Projektordner.
                # homePath statt dirname(fileName()): ein im GeoPackage
                # gespeichertes Projekt heisst "geopackage:...?projectName=",
                # und ein in den Projekteigenschaften gesetzter Home-Ordner
                # soll gewinnen (gemessen unter QGIS 3.40).
                vorschlag = os.path.join(QgsProject.instance().homePath(),
                                         vorschlag)
            pfad, gewaehlt = QtWidgets.QFileDialog.getSaveFileName(
                self, tr("save_title", self._lang), vorschlag, filter_text)
            if not pfad:
                return
        endung = os.path.splitext(pfad)[1].lower().lstrip(".")
        if endung not in filter_endung.values():
            # Keine bekannte Endung getippt: die des gewaehlten Filters
            # anhaengen, sonst entstuende eine Datei ohne Format.
            endung = filter_endung.get(gewaehlt, vorgabe)
            pfad = f"{pfad}.{endung}"
        georeferenziert = export.ist_geotiff(endung)

        try:
            geo = self._geo_angaben() if georeferenziert else None
            if georeferenziert:
                self._geotiff_schreiben(pfad, daten, geo)
            else:
                export.speichern(pfad, daten, self._dpr, geo)
        except ImportError as e:
            # Eigene Meldung: "No module named 'osgeo'" sagt niemandem,
            # dass hier die GDAL-Anbindung der QGIS-Installation fehlt und
            # dass alle anderen Formate weiterhin gehen.
            self.iface.messageBar().pushMessage(
                "Weather Picker", tr("save_err_gdal", self._lang, msg=str(e)),
                level=Qgis.Warning)
            return
        except Exception as e:
            self.iface.messageBar().pushMessage(
                "Weather Picker",
                tr("save_err", self._lang, msg=f"{pfad} ({e})"),
                level=Qgis.Warning)
            return

        if laden and georeferenziert:
            # Eigener Fehlerpfad: die Datei steht bereits korrekt auf der
            # Platte. "Speichern fehlgeschlagen" waere hier schlicht falsch.
            try:
                self._in_karte_laden(pfad)
            except Exception as e:
                self.iface.messageBar().pushMessage(
                    "Weather Picker",
                    tr("load_err", self._lang, path=pfad, msg=str(e)),
                    level=Qgis.Warning)
                return

        self.iface.messageBar().pushMessage(
            "Weather Picker", tr("saved_ok", self._lang, path=pfad),
            level=Qgis.Success)

    def _copy_clipboard(self) -> None:
        if self._img is None:
            return
        QtWidgets.QApplication.clipboard().setImage(self._img)
        self.iface.messageBar().pushMessage(
            "Weather Picker", tr("copied_ok", self._lang), level=Qgis.Info
        )
