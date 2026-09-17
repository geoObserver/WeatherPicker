"""Weather Picker - Ausgabe des Diagramms in verschiedene Dateiformate.

Buendelt alles, was Dateien schreibt, an einer Stelle. ``chart.py`` bleibt
damit reine Zeichenlogik und weiss nichts von Pfaden, Dateifiltern oder
Tabellen; ``window.py`` weiss nichts von QSvgGenerator und QPdfWriter.

Vier Formate, drei davon dasselbe Diagramm auf einem anderen Ausgabegeraet:

- **PNG** - Pixelbild in Geraete-Pixeldichte, wie im Fenster.
- **SVG** - Vektor, beliebig skalierbar und in einem Grafikprogramm
  nachbearbeitbar.
- **PDF** - Vektor auf einer Seite genau in Diagrammgroesse, fuer Bericht
  und Druck.
- **CSV** - nicht das Bild, sondern die Reihen dahinter. Wer nachrechnen
  oder weiterverarbeiten will, braucht Zahlen, kein Bild.
"""
from __future__ import annotations

import csv
import datetime
import io
import os

from qgis.PyQt import QtCore, QtGui

from . import chart, georef
from .i18n import tr

# Zoll je Millimeter, fuer die PDF-Seitengroesse.
_MM_JE_ZOLL = 25.4
# Bezugsdichte: ein logisches Pixel des Diagramms ist 1/96 Zoll breit.
# Die PDF-Seite rechnet damit ein logisches Pixel auf genau einen
# PDF-Punkt um - dieselben Proportionen wie das Bild, ohne
# Umrechnungsfehler an den Raendern -, und das GeoTIFF leitet daraus
# seine Skalierung ab.
_LOGISCHE_DPI = 96
# Druckdichte des GeoTIFF. Ein Diagramm auf Bildschirmdichte wird im
# Drucklayout und beim Hineinzoomen in die Karte sichtbar grob; 300 dpi
# ist die uebliche Untergrenze fuer Druck. Die Zahl wirkt als
# Mindestdichte: hat der Schirm mehr, gewinnt der Schirm.
GEOTIFF_DPI = 300.0


# Endungen, die ein georeferenziertes Raster bedeuten. An einer Stelle,
# weil Speichern, Georechnung und Laden in die Karte dieselbe Frage
# stellen - dreimal getippt liefen sie auseinander.
GEO_ENDUNGEN = frozenset({"tif", "tiff"})


def ist_geotiff(endung: str) -> bool:
    return endung.lower().lstrip(".") in GEO_ENDUNGEN


def formate(lang: str) -> list[tuple[str, str]]:
    """Angebotene Formate als ``(Endung, Dateifilter)``, in Anzeigereihenfolge.

    Die Reihenfolge bestimmt den Vorgabefilter im Speichern-Dialog: PNG
    zuerst, weil es das bisherige Verhalten ist."""
    return [
        ("png", tr("filter_png", lang)),
        ("svg", tr("filter_svg", lang)),
        ("pdf", tr("filter_pdf", lang)),
        ("csv", tr("filter_csv", lang)),
        ("tif", tr("filter_tif", lang)),
    ]


# Anzeigename je Endung. Nur dort noetig, wo Dateiendung und gelaeufiger
# Name auseinandergehen: "Als TIF speichern" sagt nicht, dass das Bild
# georeferenziert ist.
_ANZEIGENAME = {"tif": "GeoTIFF"}


def anzeigename(endung: str) -> str:
    return _ANZEIGENAME.get(endung, endung.upper())


def endung_zu_filter(lang: str) -> dict[str, str]:
    return dict(formate(lang))


def filter_zu_endung(lang: str) -> dict[str, str]:
    return {f: e for e, f in formate(lang)}


def speichern(pfad: str, daten: dict, dpr: float = 1.0,
              geo: dict | None = None) -> None:
    """Diagramm bzw. Reihen nach ``pfad`` schreiben; Format folgt der Endung.

    ``daten`` ist das Ergebnis von ``chart.zeichendaten()`` - also bereits
    aufs Sichtfenster geschnitten. Der Export zeigt damit genau das, was im
    Fenster steht, und nicht heimlich die volle Reihe."""
    endung = os.path.splitext(pfad)[1].lower().lstrip(".")
    if ist_geotiff(endung):
        _als_geotiff(pfad, daten, dpr, geo)
    elif endung == "svg":
        _als_svg(pfad, daten)
    elif endung == "pdf":
        _als_pdf(pfad, daten)
    elif endung == "csv":
        _als_csv(pfad, daten)
    else:
        _als_png(pfad, daten, dpr)


def vorlaeufiger_pfad(pfad: str) -> str:
    """Nachbarpfad zum Schreiben, bevor die Datei ihren Platz einnimmt.

    Die Endung bleibt, wo sie ist: ``speichern`` waehlt das Format nach
    ihr aus. Ein angehaengtes ".teil" machte aus einem GeoTIFF still ein
    PNG - die Datei hiess hinterher .tif, trug aber weder Georeferenz
    noch Druckdichte."""
    stamm, endung = os.path.splitext(pfad)
    return f"{stamm}.teil{endung}"


def diagramm_ablegen(ordner: str, daten: dict, lat: float, lon: float,
                     zeitpunkt: datetime.datetime | None = None,
                     dpr: float = 2.0) -> str:
    """Diagramm als PNG in ``ordner`` ablegen und den Pfad liefern.

    Fuer das Kartensymbol des Sammel-Layers gedacht. Der Dateiname traegt
    Koordinate und Abfragezeit, damit jeder Punkt sein eigenes Bild behaelt:
    wuerde je Koordinate ueberschrieben, zeigte ein alter Eintrag
    stillschweigend neue Daten, obwohl seine Zeitspalte etwas anderes sagt.

    Gerendert wird in doppelter Pixeldichte - das Bild wird auf der Karte
    und erst recht in einem Drucklayout groesser dargestellt als im
    Fenster."""
    name = _basisname(lat, lon, zeitpunkt or datetime.datetime.now(),
                      mit_sekunden=True) + ".png"
    # Schraegstriche statt Backslashes: der Pfad landet im Map-Tip in einer
    # file:///-URL, und gemischte Trenner ueberleben das nicht.
    pfad = os.path.join(ordner, name).replace("\\", "/")
    _als_png(pfad, daten, dpr)
    return pfad


# --- Benennung ---------------------------------------------------------------

def stempel_aus(daten: dict | None) -> datetime.datetime:
    """Zeitpunkt, auf den sich ein Ergebnis bezieht.

    Die Ortszeit am Punkt, nicht die Uhrzeit des Rechners: sie steht so
    auch im Diagrammkopf. Fehlt sie, oder steht dort etwas anderes als
    ein Zeitpunkt - alter Datensatz, Aufruf aus einem Skript -, tut es
    die aktuelle Zeit; sie erfuellt denselben Zweck, naemlich zwei
    Abrufe unterscheidbar zu machen."""
    wann = (daten or {}).get("now_local")
    return wann if isinstance(wann, datetime.datetime) \
        else datetime.datetime.now()


def _basisname(lat: float, lon: float, stempel: datetime.datetime,
               mit_sekunden: bool = False) -> str:
    """Gemeinsamer Rumpf aller Diagramm-Dateinamen: Koordinate und Zeit.

    An einer Stelle, weil der Sammel-Layer und der Speichern-Dialog
    denselben Namen bauen. Als beide ihn einzeln bauten, ging eine
    Vorzeichen-Ersetzung des einen Weges daneben und traf statt der
    Koordinate den Zeitstempel.

    Sekunden nur dort, wo sie gebraucht werden: ein Processing-Lauf legt
    mehrere Punkte je Minute ab, der Dialog fragt einmal."""
    form = "%Y%m%d-%H%M%S" if mit_sekunden else "%Y%m%d-%H%M"
    return f"weather_{lat:.4f}_{lon:.4f}_{stempel:{form}}"


def dateiname(lat: float, lon: float, stempel: datetime.datetime, *,
              endung: str, view: tuple | None = None) -> str:
    """Vorschlag fuer den Dateinamen: Koordinate, Abrufzeit, Ausschnitt.

    Die Abrufzeit gehoert dazu, weil eine Vorhersage altert. Ohne sie
    schlaegt der Dialog fuer dieselbe Stelle immer denselben Namen vor,
    und die zweite Abfrage ueberschreibt die erste - samt des bereits
    geladenen Rasterlayers, der dann still andere Daten zeigt.

    Doppelpunkte gehen nicht: Windows verbietet sie in Dateinamen.

    ``endung`` und ``view`` nur benannt: die drei Angaben davor teilt
    sich diese Funktion mit ``layer_name``, und vertauschte Argumente
    faenden weder Python noch ein Test - beide Namen waeren nur
    falsch."""
    name = _basisname(lat, lon, stempel)
    if view is not None:
        von, bis = view
        name += f"_{von:%Y-%m-%d-%Hh}_{bis:%Y-%m-%d-%Hh}"
    return f"{name}.{endung}"


def layer_name(lat: float, lon: float, stempel: datetime.datetime, *,
               ort: str | None = None,
               view: tuple | None = None) -> str:
    """Name des Rasterlayers in der Layerliste.

    Ortsname, sonst die Koordinate - der Diagrammtitel stuende bei jedem
    Layer gleich da. Dahinter die Abrufzeit: zwei Vorhersagen derselben
    Stelle sind zwei verschiedene Bilder, und ohne Zeit heissen beide
    Layer gleich.

    Datum und Uhrzeit in ISO-Schreibweise, weil sich die Layerliste
    danach richtig sortieren laesst."""
    basis = ort or f"{lat:.4f} {lon:.4f}"
    name = f"{basis} {stempel:%Y-%m-%d %H:%M}"
    if view is not None:
        von, bis = view
        name += f" ({von:%m-%d %Hh}-{bis:%m-%d %Hh})"
    return name


# --- Bildformate -------------------------------------------------------------

def _diagramm_bild(daten: dict, dpr: float) -> tuple[QtGui.QImage, float]:
    """Gerendertes Diagramm als QImage plus die verwendete Skalierung.

    Ohne ``setDevicePixelRatio`` - das gehoert nur ans PNG, wo es der
    Anzeige sagt, dass das Bild in doppelter Dichte vorliegt. Im GeoTIFF
    waere die Angabe bedeutungslos, dort zaehlt allein die Georeferenz."""
    skalierung = max(1.0, dpr)
    img = QtGui.QImage(
        max(1, int(chart.LOGICAL_W * skalierung)),
        max(1, int(chart.LOGICAL_H * skalierung)),
        QtGui.QImage.Format.Format_ARGB32,
    )
    img.fill(QtGui.QColor("white"))
    chart.zeichnen_auf(img, daten, skalierung)
    return img, skalierung


def _als_png(pfad: str, daten: dict, dpr: float) -> None:
    """Pixelbild in Geraete-Pixeldichte, damit es auf HiDPI-Schirmen scharf
    bleibt."""
    img, skalierung = _diagramm_bild(daten, dpr)
    img.setDevicePixelRatio(skalierung)
    if not img.save(pfad, "PNG"):
        raise OSError(pfad)


def _als_svg(pfad: str, daten: dict) -> None:
    """Vektorfassung. Keine Geraete-Pixeldichte noetig - ein Vektorbild ist
    in jeder Groesse scharf."""
    from qgis.PyQt.QtSvg import QSvgGenerator

    gen = QSvgGenerator()
    gen.setFileName(pfad)
    gen.setSize(QtCore.QSize(chart.LOGICAL_W, chart.LOGICAL_H))
    gen.setViewBox(QtCore.QRect(0, 0, chart.LOGICAL_W, chart.LOGICAL_H))
    gen.setTitle(daten.get("place") or "Weather Picker")
    # Der Hintergrund gehoert mitgezeichnet: anders als beim QImage gibt es
    # bei SVG keine Grundfarbe, das Diagramm laege sonst auf Durchsicht und
    # waere auf dunklem Untergrund unlesbar.
    chart.zeichnen_auf(gen, _mit_hintergrund(daten))


def _als_pdf(pfad: str, daten: dict) -> None:
    """Eine Seite, genau so gross wie das Diagramm - kein Papierformat mit
    Rand drumherum, das beim Einbinden wieder beschnitten werden muesste."""
    schreiber = QtGui.QPdfWriter(pfad)
    schreiber.setResolution(_LOGISCHE_DPI)
    schreiber.setPageSize(QtGui.QPageSize(
        QtCore.QSizeF(chart.LOGICAL_W / _LOGISCHE_DPI * _MM_JE_ZOLL,
                      chart.LOGICAL_H / _LOGISCHE_DPI * _MM_JE_ZOLL),
        QtGui.QPageSize.Unit.Millimeter))
    schreiber.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))
    schreiber.setCreator("Weather Picker")
    chart.zeichnen_auf(schreiber, _mit_hintergrund(daten))


def _als_geotiff(pfad: str, daten: dict, dpr: float, geo: dict | None) -> None:
    """Diagramm als georeferenziertes Raster am Abfragepunkt.

    ``geo`` traegt Ziel-CRS, Bodenbreite und die Koordinate des Punktes in
    diesem CRS - gerechnet in ``georef``, erfragt in ``window``. Ohne
    ``geo`` waere das Bild nur ein TIFF ohne Raumbezug; dann lieber ein
    klarer Fehler als eine Datei, die sich spaeter irgendwo auf der Welt
    wiederfindet.

    Der Weg fuehrt ueber ein PNG im Arbeitsspeicher (``/vsimem``) statt
    ueber eine Zwischendatei auf der Platte: das erspart Aufraeumen und
    einen Pfad, der bei einer Ausnahme liegen bliebe.

    Der Untergrund bleibt weiss und deckend. Ein Diagramm ueber einer
    Hintergrundkarte ohne Ruecken ist unlesbar, und die freigestellten
    Beschriftungen (Sigma-Endwert, "jetzt") setzen Weiss hinter sich
    voraus."""
    if not geo:
        raise ValueError("GeoTIFF ohne Georeferenz angefordert")
    from osgeo import gdal

    # Mindestens Druckdichte, nicht Bildschirmdichte: das Bild steht
    # hinterher in der Karte und im Drucklayout, wo es vergroessert wird.
    # Auf die Georeferenz wirkt das nicht - die Bodenbreite bleibt
    # dieselbe, nur die Bodenaufloesung wird feiner.
    dichte = max(dpr, GEOTIFF_DPI / _LOGISCHE_DPI)
    img, skalierung = _diagramm_bild(daten, dichte)
    puffer = QtCore.QBuffer()
    puffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(puffer, "PNG"):
        raise OSError("Diagramm liess sich nicht in den Puffer schreiben")
    puffer.close()
    # Nur noch die Masse werden gebraucht. Das Bild selbst belegt in
    # Druckdichte rund 36 MB; es bis zum Ende des GDAL-Weges festzuhalten
    # haette keinen Zweck.
    breite, hoehe = img.width(), img.height()
    del img

    # gdal.UseExceptions() ist ein prozessweiter Schalter auf dem Modul, das
    # sich QGIS und jedes andere Plugin teilen. Bliebe er an, bekaeme
    # fremder Code, der bei einem Fehler None erwartet, ploetzlich einen
    # RuntimeError. Deshalb nur fuer diesen Aufruf setzen und danach den
    # vorgefundenen Zustand wiederherstellen.
    vorher = gdal.GetUseExceptions()
    gdal.UseExceptions()
    speicherpfad = f"/vsimem/weatherpicker_{id(puffer):x}.png"
    gdal.FileFromMemBuffer(speicherpfad, bytes(puffer.data()))
    quelle = ziel = None
    try:
        quelle = gdal.Open(speicherpfad)
        ziel = gdal.GetDriverByName("GTiff").CreateCopy(
            pfad, quelle, options=["COMPRESS=DEFLATE", "PHOTOMETRIC=RGB"])
        ziel.SetGeoTransform(georef.geotransform(
            geo["x"], geo["y"], geo["breite_m"], breite, hoehe))
        # WKT direkt: der Umweg ueber osr.SpatialReference waere ein reiner
        # Roundtrip durch dieselbe Zeichenkette.
        ziel.SetProjection(geo["crs"].toWkt())
        # Aufloesungs-Tags des TIFF. Fuer die Karte belanglos, dort zaehlt
        # die Georeferenz - aber ein Layout- oder Bildprogramm liest daraus
        # die Druckgroesse, und ohne die Angabe nimmt es 72 dpi an und legt
        # das Diagramm ueber einen halben Meter breit an.
        ziel.SetMetadataItem("TIFFTAG_XRESOLUTION",
                             f"{skalierung * _LOGISCHE_DPI:.0f}")
        ziel.SetMetadataItem("TIFFTAG_YRESOLUTION",
                             f"{skalierung * _LOGISCHE_DPI:.0f}")
        ziel.SetMetadataItem("TIFFTAG_RESOLUTIONUNIT", "2")   # 2 = Zoll
        # Explizit schliessen: GDAL schreibt erst beim Aufloesen des
        # Datasets, und der Aufrufer laedt die Datei gleich danach.
        ziel = None
        quelle = None
    except Exception:
        # Nach einem erfolgreichen CreateCopy liegt die Datei bereits da.
        # Schlaegt erst das Setzen der Georeferenz fehl, bliebe ein TIFF
        # ohne Raumbezug zurueck - und der Anwender bekaeme die Meldung
        # "Speichern fehlgeschlagen" zu einer Datei, die es gibt.
        ziel = None
        quelle = None
        try:
            if os.path.exists(pfad):
                os.remove(pfad)
        except OSError:
            pass
        raise
    finally:
        gdal.Unlink(speicherpfad)
        if not vorher:
            gdal.DontUseExceptions()


def _mit_hintergrund(daten: dict) -> dict:
    """Kopie der Zeichendaten mit weissem Untergrund.

    SVG und PDF haben keine Grundfarbe; das QImage bekommt sie vorher per
    ``fill``. Statt den Maler um einen Sonderfall zu erweitern, wird hier
    ein Flag gesetzt, das er zu Beginn auswertet."""
    kopie = dict(daten)
    kopie["untergrund"] = QtGui.QColor("white")
    return kopie


# --- Datenformat -------------------------------------------------------------

def _als_csv(pfad: str, daten: dict) -> None:
    """Die Reihen hinter dem Bild als Tabelle.

    Trennzeichen und Dezimaltrenner folgen der Oberflaechensprache, wie
    ueberall sonst im Plugin: Deutsch Semikolon und Komma, Englisch Komma
    und Punkt. So laesst sich die Datei ohne Import-Assistenten in einer
    Tabellenkalkulation oeffnen, die dieselbe Spracheinstellung hat."""
    with open(pfad, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(als_csv_text(daten))


def als_csv_text(daten: dict) -> str:
    """CSV-Inhalt als Zeichenkette. Getrennt vom Dateizugriff, damit sich
    der Aufbau ohne Dateisystem testen laesst."""
    lang = daten.get("lang", "de")
    u = daten.get("u") or {}
    deutsch = lang == "de"
    trenner = ";" if deutsch else ","

    def zahl(wert):
        """Zahl mit dem Dezimaltrenner der Sprache; None bleibt leer."""
        if wert is None:
            return ""
        text = f"{float(wert):.2f}"
        return text.replace(".", ",") if deutsch else text

    tdts = daten.get("tdts") or []
    temp = daten.get("temp") or []
    precip = daten.get("precip") or []
    mittel = daten.get("temp_mittel") or []
    kum = daten.get("kum_werte") or []
    times = daten.get("times") or []

    puffer = io.StringIO()
    schreiber = csv.writer(puffer, delimiter=trenner, lineterminator="\r\n")
    schreiber.writerow([
        tr("csv_time", lang),
        f"{tr('csv_temp', lang)} [{u.get('temp', '')}]",
        f"{tr('csv_precip', lang)} [{u.get('precip', '')}]",
        f"{tr('csv_mean', lang)} [{u.get('temp', '')}]",
        f"{tr('csv_cumulative', lang)} [{u.get('precip', '')}]",
    ])
    for i in range(len(temp)):
        # Zeitstempel in ISO 8601, unabhaengig von der Sprache: das ist die
        # Spalte, die eine Tabellenkalkulation oder ein Skript wieder
        # einlesen soll, und ein lokalisiertes Datum waere dort mehrdeutig.
        if i < len(tdts) and isinstance(tdts[i], datetime.datetime):
            zeit = tdts[i].isoformat(timespec="minutes")
        else:
            zeit = times[i] if i < len(times) else ""
        schreiber.writerow([
            zeit,
            zahl(temp[i]),
            zahl(precip[i] if i < len(precip) else None),
            zahl(mittel[i] if i < len(mittel) else None),
            zahl(kum[i] if i < len(kum) else None),
        ])
    return puffer.getvalue()
