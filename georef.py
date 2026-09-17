"""Weather Picker - Georeferenzierung des Diagramms.

Reine Rechnung, keine Dateien und keine GUI: welches Ziel-CRS, wie breit,
und welche Affin-Transformation setzt das Bild an den Abfragepunkt. Das
Schreiben der Datei liegt in ``export.py``, das Erfragen von Kartenzustand
in ``window.py``.

Ein Diagramm ist keine Flaeche auf dem Boden. Es in Kartenkoordinaten zu
legen heisst, ihm eine Bodengroesse zu geben - beim Herauszoomen wird es
dann zum Fleck, beim Hineinzoomen bildschirmfuellend. Fuer die Ansicht in
QGIS ist deshalb das Kartensymbol (``points_layer``) die bessere Antwort;
das GeoTIFF ist fuer Weitergabe, Ueberlagerung und andere Software da,
wo eine Datei mit echtem Raumbezug gebraucht wird.
"""
from __future__ import annotations

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
)

# Anteil der Kartenbreite, den das Diagramm einnimmt. Die Bodengroesse aus
# dem aktuellen Ausschnitt abzuleiten ist der einzige Weg, der ohne Rueckfrage
# auskommt: das Bild ist danach so gross, wie es beim Export ausgesehen hat.
ANTEIL_KARTENBREITE = 0.25
# Rueckfall, wenn kein Kartenausschnitt vorliegt (Skript, Test). Zehn
# Kilometer sind auf Gemeindeebene eine brauchbare Groesse.
BREITE_OHNE_KARTE_M = 10000.0


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG-Code der UTM-Zone, in der dieser Punkt liegt.

    Zonen sind 6 Grad breit und ab 180 West durchnummeriert; 326xx ist die
    Nordhalbkugel, 327xx die Suedhalbkugel."""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(60, max(1, zone))
    return (32600 if lat >= 0 else 32700) + zone


def ziel_crs(karten_crs: QgsCoordinateReferenceSystem | None,
             lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    """CRS fuer das GeoTIFF: das der Karte, sofern projiziert.

    In einem geografischen CRS ist ein Pixel kein Quadrat - ein Grad
    Laenge ist um ``cos(Breite)`` kuerzer als ein Grad Breite. Das Diagramm
    erschiene dort verzerrt (in Mitteleuropa rund 37 % zu breit). Deshalb
    in dem Fall auf die passende UTM-Zone ausweichen, statt ein schiefes
    Bild zu schreiben."""
    if (karten_crs is not None and karten_crs.isValid()
            and not karten_crs.isGeographic()):
        return karten_crs
    return QgsCoordinateReferenceSystem.fromEpsgId(utm_epsg(lon, lat))


def breite_aus_ausschnitt(ausschnitt: QgsRectangle | None,
                          anteil: float = ANTEIL_KARTENBREITE) -> float:
    """Bodenbreite des Diagramms aus dem sichtbaren Kartenausschnitt.

    Der Ausschnitt muss bereits im Ziel-CRS vorliegen, sonst kaeme bei
    einer geografischen Karte eine Breite in Grad heraus."""
    if ausschnitt is None or ausschnitt.width() <= 0:
        return BREITE_OHNE_KARTE_M
    return ausschnitt.width() * anteil


class GeorefFehler(Exception):
    """Die Georeferenz laesst sich nicht bestimmen.

    Eigene Ausnahme statt eines stillen ``None``: eine kaputte
    PROJ-Konfiguration oder ein ungueltiges Ziel-CRS soll den Anwender mit
    dem urspruenglichen Grund erreichen, nicht als allgemeines "ohne
    Georeferenz" enden."""


def punkt_im_ziel_crs(lon: float, lat: float,
                      ziel: QgsCoordinateReferenceSystem):
    """WGS84-Koordinate ins Ziel-CRS bringen."""
    from qgis.core import QgsPointXY
    quelle = QgsCoordinateReferenceSystem("EPSG:4326")
    if not quelle.isValid() or not ziel.isValid():
        raise GeorefFehler(f"ungueltiges CRS: {ziel.authid() or ziel.toWkt()[:40]}")
    try:
        umrechnung = QgsCoordinateTransform(quelle, ziel, QgsProject.instance())
        return umrechnung.transform(QgsPointXY(lon, lat))
    except Exception as e:
        raise GeorefFehler(f"{type(e).__name__}: {e}") from e


def geotransform(x: float, y: float, breite_m: float,
                 bild_breite: int, bild_hoehe: int) -> list[float]:
    """Affine Transformation im GDAL-Format fuer ein Bild ueber dem Punkt.

    Anker ist die **untere Mitte** des Bildes: das Diagramm steht auf dem
    Abfragepunkt, statt ihn zu verdecken - dieselbe Anordnung wie beim
    Kartensymbol, damit beide Darstellungen zusammenpassen.

    Reihenfolge nach GDAL: ``[links, px_x, dreh_x, oben, dreh_y, px_y]``.
    Die Drehglieder sind 0 (nordgerichtet), ``px_y`` ist negativ, weil die
    Bildzeilen nach Sueden laufen."""
    if bild_breite <= 0 or bild_hoehe <= 0 or breite_m <= 0:
        raise ValueError("geotransform: Bildmasse und Breite muessen positiv sein")
    px = breite_m / float(bild_breite)
    links = x - breite_m / 2.0
    oben = y + bild_hoehe * px
    return [links, px, 0.0, oben, 0.0, -px]


def geo_angaben(karten_crs: QgsCoordinateReferenceSystem | None,
                karten_ausschnitt: QgsRectangle | None,
                lon: float, lat: float) -> dict:
    """Alles, was ``export`` fuer ein GeoTIFF braucht, in einem Dict.

    Wirft ``GeorefFehler``, wenn sich der Punkt nicht ins Ziel-CRS bringen
    laesst: lieber gar kein georeferenziertes Bild mit nennbarem Grund als
    eines an der falschen Stelle."""
    ziel = ziel_crs(karten_crs, lon, lat)
    punkt = punkt_im_ziel_crs(lon, lat, ziel)

    ausschnitt = karten_ausschnitt
    if (ausschnitt is not None and karten_crs is not None
            and karten_crs.isValid() and karten_crs != ziel):
        # Der Ausschnitt kommt im CRS der Karte. Ohne Umrechnung waere die
        # Breite in Grad und das Bild um Groessenordnungen daneben.
        try:
            umrechnung = QgsCoordinateTransform(karten_crs, ziel,
                                                QgsProject.instance())
            ausschnitt = umrechnung.transformBoundingBox(ausschnitt)
        except Exception:
            ausschnitt = None

    return {
        "crs": ziel,
        "breite_m": breite_aus_ausschnitt(ausschnitt),
        "x": punkt.x(),
        "y": punkt.y(),
    }
