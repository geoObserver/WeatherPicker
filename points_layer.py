"""Weather Picker - optionaler Sammel-Layer fuer angeklickte/gesuchte Punkte.

Ein einzelner Memory-Punkt-Layer (EPSG:4326) wird bei Bedarf angelegt und mit
je einem Feature pro angezeigtem Wetter-Ergebnis gefuellt. Wiedergefunden wird
er ueber eine Custom-Property, sodass ein vom Anwender entfernter Layer einfach
neu erzeugt wird.
"""
from __future__ import annotations

import datetime
import os

from qgis.core import (
    Qgis,
    QgsAction,
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsMarkerSymbolLayer,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRasterMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant

from .i18n import tr, weather_code_text

# Qt6/QGIS-3.38+: QgsField(name, QVariant.Type) ist deprecated und auf Qt6-Builds
# aktiv gefaehrlich (Memory-Layer-Crash). Ab 3.38 die QMetaType-Signatur nutzen,
# darunter die alte QVariant-Form (runtime-gated, kein Source-Branch noetig).
if Qgis.QGIS_VERSION_INT >= 33800:
    from qgis.PyQt.QtCore import QMetaType
    _STR, _DBL = QMetaType.Type.QString, QMetaType.Type.Double
else:  # QGIS < 3.38 (Qt5)
    _STR, _DBL = QVariant.String, QVariant.Double

_PROP = "weather_picker_points"   # Custom-Property zum Wiederfinden des Layers
# Unterordner fuer die abgelegten Diagramme, neben dem Projekt bzw. im Profil.
_BILDORDNER = "weather_picker_charts"
# Breite des Diagramm-Symbols auf der Karte. In Millimetern, nicht in
# Karteneinheiten: ein Diagramm ist keine Flaeche auf dem Boden, es soll in
# jeder Zoomstufe gleich gross und lesbar bleiben.
_SYMBOL_BREITE_MM = 45.0
# Breite des Diagramms im Map-Tip. Die Sprechblase waechst mit; darueber
# passt sie auf kleinen Schirmen nicht mehr neben den Punkt.
_MAPTIP_BREITE_PX = 520
# Kennung der Layer-Aktion, damit ein zweiter Lauf sie nicht doppelt anlegt.
_AKTION_ID = "weather_picker_chart"
# Feldname des Diagrammpfads. Er taucht als Feld, als datengesteuerte
# Eigenschaft, in einem QGIS-Ausdruck und als dict-Schluessel auf - in vier
# Schreibweisen an einer Stelle festgehalten.
FELD_CHART = "chart"
# Custom-Property, die festhaelt, dass Symbol und Map-Tip einmal gesetzt
# wurden.
_PROP_AUSGESTATTET = "weather_picker_styled"
# Map-Tip-Vorlagen frueherer Plugin-Staende. Nur wer eine davon vorfindet,
# darf sie ersetzen - alles andere hat der Anwender selbst geschrieben.
# Wird die Vorlage erneut geaendert, gehoert die bisherige hier hinein.
_MAPTIP_FRUEHER = (
    f'<img src="file:///[% "{FELD_CHART}" %]" width="{_MAPTIP_BREITE_PX}">',
)


def bildordner() -> str:
    """Ablageort der Diagramme. Niemals das Temp-Verzeichnis.

    QGIS haelt jeden Layer fuer temporaer, dessen Quelle unter einem
    TempLocation-Eintrag liegt, und nimmt ihn beim Leeren des Projekts in
    einen eigenen Teardown-Pfad - auf Windows ein bekannter Absturzweg.
    Bilder sind zwar keine Layer-Quelle, aber derselbe Ort waere auch hier
    die falsche Wahl: die Dateien verschwaenden unter den Fuessen des
    Projekts. Gespeichertes Projekt -> daneben, sonst Profilordner."""
    projekt = QgsProject.instance().fileName()
    basis = (os.path.dirname(projekt) if projekt
             else QgsApplication.qgisSettingsDirPath())
    ordner = os.path.join(basis, _BILDORDNER)
    os.makedirs(ordner, exist_ok=True)
    return ordner


def _build_fields() -> QgsFields:
    """Felder des Sammel-Layers. Alles ausser Text als Double (humidity/wind etc.
    dann ohne Int-Koerzierungs-Stolperfallen, NULL bei fehlenden Werten)."""
    fields = QgsFields()
    for name, qtype in (
        ("time", _STR), ("place", _STR),
        ("lon", _DBL), ("lat", _DBL),
        ("temp", _DBL), ("feels", _DBL), ("humidity", _DBL), ("wind", _DBL),
        ("t_min", _DBL), ("t_max", _DBL), ("precip", _DBL),
        ("weather", _STR),
        # Pfad zum abgelegten Diagramm. Das Symbol liest ihn datengesteuert,
        # damit jeder Punkt sein eigenes Bild traegt.
        ("chart", _STR),
    ):
        fields.append(QgsField(name, qtype))
    return fields


def _find_layer() -> QgsVectorLayer | None:
    for layer in QgsProject.instance().mapLayers().values():
        if (isinstance(layer, QgsVectorLayer) and layer.isValid()
                and layer.customProperty(_PROP) == "1"):
            return layer
    return None


def ensure_layer(name: str, lang: str = "de") -> QgsVectorLayer:
    """Den Sammel-Layer holen oder einmalig anlegen und ins Projekt haengen."""
    layer = _find_layer()
    if layer is not None:
        _ausstatten(layer, lang)
        return layer
    layer = QgsVectorLayer("Point?crs=EPSG:4326", name, "memory")
    layer.setCustomProperty(_PROP, "1")
    # Felder legt _ausstatten ueber _felder_nachruesten an; bei einem
    # frischen Layer fehlen schlicht alle.
    _ausstatten(layer, lang)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _ausstatten(layer: QgsVectorLayer, lang: str = "de") -> None:
    """Symbol, Map-Tip und Layer-Aktion setzen - mehrfach aufrufbar.

    Auch fuer einen wiedergefundenen Layer: der kann aus einem aelteren
    Projekt stammen, das diese Ausstattung noch nicht kannte.

    Symbol und Map-Tip aber nur einmal. Sie bedingungslos bei jedem Klick
    neu zu setzen wuerde eine vom Anwender in QGIS geaenderte
    Symbolisierung still zuruecksetzen - das Plugin wuerde ihm bei jeder
    Abfrage ins Layout pfuschen. Die Felder werden weiterhin jedes Mal
    abgeglichen, denn ein fehlendes Feld ist ein Fehler, keine
    Geschmacksfrage."""
    _felder_nachruesten(layer)
    _aktion_setzen(layer, lang)
    if layer.customProperty(_PROP_AUSGESTATTET) == "1":
        _maptip_nachziehen(layer)
        return
    diagramm_symbol_setzen(layer)
    _maptip_setzen(layer)
    layer.setCustomProperty(_PROP_AUSGESTATTET, "1")


def _maptip_nachziehen(layer: QgsVectorLayer) -> bool:
    """Eine aeltere Plugin-Vorlage durch die aktuelle ersetzen.

    Der Einmal-Riegel oben schuetzt die Handschrift des Anwenders, hielt
    aber auch Korrekturen an der Vorlage von jedem Layer fern, der schon
    ausgestattet war - gerade das Projekt aus dem letzten Monat zeigte
    also weiter das Kaputtes-Bild-Zeichen, das die neue Vorlage gerade
    vermeidet.

    Ersetzt wird darum genau dann, wenn dort noch eine frueher vom
    Plugin gesetzte Vorlage steht. Hat der Anwender eigenen Text
    eingetragen, bleibt er unberuehrt."""
    if layer.mapTipTemplate() not in _MAPTIP_FRUEHER:
        return False
    _maptip_setzen(layer)
    return True


def _felder_nachruesten(layer: QgsVectorLayer) -> list[str]:
    """Alle Felder ergaenzen, die dem wiedergefundenen Layer fehlen.

    Ein Layer aus einem aelteren gespeicherten Projekt kennt die seither
    hinzugekommenen Felder nicht - zuletzt ``chart``. ``add_point`` wuerde
    beim Zuweisen mit einem KeyError abbrechen, und zwar erst beim
    naechsten Klick des Anwenders, nicht hier. Deshalb gegen den
    vollstaendigen Feldsatz abgleichen statt gegen ein einzelnes Feld:
    das deckt auch jede kuenftige Erweiterung ab."""
    vorhanden = {f.name() for f in layer.fields()}
    fehlend = [f for f in _build_fields() if f.name() not in vorhanden]
    if not fehlend:
        return []
    layer.dataProvider().addAttributes(fehlend)
    layer.updateFields()
    return [f.name() for f in fehlend]


def _maptip_setzen(layer: QgsVectorLayer) -> None:
    """Diagramm als Sprechblase beim Ueberfahren mit der Maus.

    Kostet nichts: der Pfad liegt bereits im Feld. Ohne das muesste man
    den Layer erst einfaerben oder die Attributtabelle oeffnen, um an das
    Bild zu kommen.

    ``file:///`` braucht Schraegstriche - deshalb legt ``diagramm_ablegen``
    die Pfade schon in dieser Form ab.

    Der Ausdruck prueft das Feld, statt die URL blind zusammenzusetzen: ist
    kein Diagramm hinterlegt - Diagramm-Erzeugung abgeschaltet, Eintrag aus
    einem aelteren Lauf - entstuende sonst ``file:///`` ohne Ziel, und die
    Sprechblase zeigte ein Kaputtes-Bild-Zeichen statt gar nichts."""
    ausdruck = (
        'CASE WHEN "{f}" IS NULL OR "{f}" = \'\' THEN \'\''
        ' ELSE \'<img src="file:///\' || "{f}" || \'" width="{b}">\' END'
    ).format(f=FELD_CHART, b=_MAPTIP_BREITE_PX)
    layer.setMapTipTemplate("[% " + ausdruck + " %]")
    # Erst ab neueren 3.x vorhanden; davor sind Map-Tips immer aktiv.
    if hasattr(layer, "setMapTipsEnabled"):
        layer.setMapTipsEnabled(True)


def _aktion_setzen(layer: QgsVectorLayer, lang: str = "de") -> None:
    """Aktion "Diagramm oeffnen" fuer Attributtabelle und Karte.

    Oeffnet das PNG im Standardprogramm des Systems - der Weg zum grossen
    Bild, wenn die Sprechblase nicht reicht."""
    aktionen = layer.actions()
    # Ueber den Kurztitel wiedererkennen: QgsAction hat keinen Setter dafuer,
    # der Wert kommt aus dem Konstruktor und bleibt stabil. Die zufaellige
    # UUID taugt dazu nicht, sie ist bei jedem Lauf eine andere.
    if any(a.shortTitle() == _AKTION_ID for a in aktionen.actions()):
        return
    aktionen.addAction(QgsAction(
        QgsAction.ActionType.OpenUrl,
        tr("action_open_chart", lang),
        f'[% "{FELD_CHART}" %]',
        "",                      # kein eigenes Symbol
        False,                   # Ausgabe nicht abfangen
        _AKTION_ID,
        {"Feature", "Canvas"},
    ))


def diagramm_symbol_setzen(layer: QgsVectorLayer) -> None:
    """Jeden Punkt sein Diagramm als Symbol tragen lassen.

    Zwei Symbolebenen: ein kleiner Punkt, der die Koordinate markiert, und
    darueber das Bild. Das Bild haengt mit seiner Unterkante am Punkt, steht
    also darueber statt darauf - sonst verdeckte es genau die Stelle, die es
    beschreibt.

    Der Pfad kommt datengesteuert aus dem Feld ``chart``. Damit braucht es
    keinen Layer je Punkt: ein Layer, ein Symbol, und trotzdem zeigt jeder
    Punkt sein eigenes Diagramm. Wo das Feld leer ist, zeichnet die Ebene
    nichts - dann bleibt nur der Punkt.

    Die Groesse steht in Millimetern. Ein Diagramm ist keine Flaeche auf dem
    Boden; in Karteneinheiten waere es beim Herauszoomen ein Fleck und beim
    Hineinzoomen bildschirmfuellend."""
    punkt = QgsMarkerSymbol.createSimple(
        {"name": "circle", "color": "#7b1fa2", "outline_color": "#ffffff",
         "outline_width": "0.3", "size": "2.4"})

    bild = QgsRasterMarkerSymbolLayer()
    bild.setSize(_SYMBOL_BREITE_MM)
    bild.setSizeUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
    bild.setVerticalAnchorPoint(QgsMarkerSymbolLayer.VerticalAnchorPoint.Bottom)
    # Property.Name, nicht Property.File: QgsRasterMarkerSymbolLayer fuehrt
    # zwar beide in propertyDefinitions() und "file" klingt nach dem Pfad,
    # gelesen wird beim Zeichnen aber Name. Mit File bleibt die Ebene
    # stumm - kein Fehler, kein Log, nur kein Bild.
    bild.setDataDefinedProperty(QgsSymbolLayer.Property.Name,
                                QgsProperty.fromField(FELD_CHART))
    punkt.appendSymbolLayer(bild)
    layer.setRenderer(QgsSingleSymbolRenderer(punkt))


def verwendete_diagramme() -> set[str]:
    """Alle Diagrammpfade, auf die noch ein Feature zeigt.

    Ueber ALLE Vektorlayer des Projekts, nicht nur ueber den Sammel-Layer:
    ein Ergebnis des Processing-Algorithmus traegt dieselben Felder, aber
    nicht die Custom-Property, ueber die der Sammel-Layer gefunden wird.
    Wer nur dort nachsaehe, loeschte unter fremden Layern weg."""
    verwendet = set()
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            continue
        if FELD_CHART not in [f.name() for f in layer.fields()]:
            continue
        for feat in layer.getFeatures():
            wert = feat[FELD_CHART]
            if wert:
                verwendet.add(os.path.normcase(os.path.abspath(str(wert))))
    return verwendet


def unbenutzte_diagramme() -> list[str]:
    """Diagrammdateien, auf die kein Feature mehr zeigt.

    Getrennt vom Loeschen, damit die Rueckfrage eine Zahl nennen kann -
    "3 Dateien loeschen?" ist beantwortbar, "aufraeumen?" nicht - und der
    Ordner trotzdem nur einmal durchsucht wird."""
    verwendet = verwendete_diagramme()
    ordner = bildordner()
    return [
        os.path.join(ordner, name)
        for name in os.listdir(ordner)
        if name.lower().endswith(".png")
        and os.path.normcase(os.path.abspath(os.path.join(ordner, name)))
        not in verwendet
    ]


def aufraeumen(kandidaten: list[str] | None = None) -> tuple[int, int]:
    """Nicht mehr verwendete Diagramme loeschen.

    Der Ordner waechst mit jeder Abfrage; geloescht wird aber nur, worauf
    kein Feature mehr zeigt. Liefert ``(geloescht, uebrig)``.

    ``kandidaten`` erlaubt es dem Aufrufer, die zuvor angezeigte Liste
    weiterzureichen, statt alle Layer ein zweites Mal zu durchsuchen.

    Je Datei abgesichert: eine Datei, die das System gerade offen haelt,
    darf den Rest des Laufs nicht verhindern."""
    if kandidaten is None:
        kandidaten = unbenutzte_diagramme()
    geloescht = uebrig = 0
    for pfad in kandidaten:
        try:
            os.remove(pfad)
            geloescht += 1
        except OSError:
            uebrig += 1
    return geloescht, uebrig


def add_point(lat: float, lon: float, place: str | None,
              extras: dict | None, lang: str, name: str,
              diagramm: str | None = None) -> QgsVectorLayer:
    """Ein Feature mit den aktuellen Werten an den Sammel-Layer anhaengen.

    ``diagramm`` ist der Pfad zu einem bereits abgelegten Diagrammbild; ohne
    Angabe bleibt das Feld leer und der Punkt zeigt nur seine Markierung."""
    layer = ensure_layer(name, lang)
    ex = extras or {}

    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
    feat["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    feat["place"] = place or ""
    feat["lon"] = round(float(lon), 6)
    feat["lat"] = round(float(lat), 6)
    feat["temp"] = ex.get("temp")
    feat["feels"] = ex.get("apparent")
    feat["humidity"] = ex.get("humidity")
    feat["wind"] = ex.get("wind")
    feat["t_min"] = ex.get("today_min")
    feat["t_max"] = ex.get("today_max")
    feat["precip"] = ex.get("today_precip")
    feat["weather"] = weather_code_text(ex.get("code"), lang)
    feat[FELD_CHART] = diagramm or ""

    layer.dataProvider().addFeature(feat)
    layer.updateExtents()
    layer.triggerRepaint()
    return layer
