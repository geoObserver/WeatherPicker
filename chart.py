"""Weather Picker - Diagramm-Rendering (QPainter auf ein QImage).

Nur Zeichenlogik; kein Netz, kein Plugin-Zustand. Scoped Qt-Enums fuer
PyQt5/PyQt6.
"""
from __future__ import annotations

import datetime
import math
import re

from qgis.PyQt import QtCore, QtGui

from .i18n import (tr, weather_code_text, _fmt_num, _format_date_label,
                    _format_datetime_label)

_MARKUP = re.compile(r"<[^>]+>")
_TRENNER = "   ·   "   # Trenner zwischen Teilstuecken einer Textzeile

# Strichmuster der kumulierten Linie. Als Konstante, weil Linie und
# Legendenmarke es teilen muessen - getrennt gepflegt liefen sie auseinander.
_STRICH_KUMULIERT = [4, 3]

# Logische Zeichenflaeche und Raender. Als Modulkonstanten, weil das
# Ergebnis-Fenster sie braucht, um eine Mausposition in eine Zeit
# umzurechnen - dort nachgebaut liefen die Werte auseinander.
LOGICAL_W, LOGICAL_H = 1280, 714
# Die seitlichen Raender tragen Teilstrich-Beschriftung UND Achsentitel.
# Seit an jedem Teilstrich die Einheit steht ("0,8 mm" statt "0,8"), braucht
# es dort mehr Platz: bei 82 px lief die Beschriftung in den senkrechten
# Titel hinein und "15 °C" erschien als "5 °C".
MARGIN_LEFT, MARGIN_RIGHT = 104, 104
MARGIN_TOP, MARGIN_BOTTOM = 132, 120
PLOT_W = LOGICAL_W - MARGIN_LEFT - MARGIN_RIGHT
PLOT_H = LOGICAL_H - MARGIN_TOP - MARGIN_BOTTOM

# --- Farbpalette (dezent-modern, aber kontraststark) -------------------------
# Auf Modulebene, nicht je Aufruf neu: die Farben sind konstant, und ein
# gezoomtes Fenster ruft render_chart bei jedem Mausrad-Schritt erneut auf.
# QColor braucht keine laufende QApplication, der Import bleibt also
# unabhaengig von QGIS (Tests importieren dieses Modul ohne GUI).
C_INK       = QtGui.QColor("#222222")          # Haupttext
C_MUTE      = QtGui.QColor("#6f6f6f")          # Sekundärtext / Datum
C_GRID      = QtGui.QColor("#e3e3e3")          # Gitternetz (beschriftet)
C_GRID_FEIN = QtGui.QColor("#e8e8e8")          # Zwischengitter (unbeschriftet)
C_AXIS      = QtGui.QColor("#bdbdbd")          # Achsenlinien
C_FROST     = QtGui.QColor("#6b8fb5")          # Null-Grad-Linie (Gefrierpunkt)
# Markantes Violett fuer den "Jetzt"-Marker: klar unterscheidbar von der
# Temperatur (Rot-Orange) und vom Regen (Blau). Linie und Beschriftung
# teilen sich diese eine Farbe, damit die Zusammengehoerigkeit sichtbar ist.
C_NOW       = QtGui.QColor("#7b1fa2")          # "Jetzt"-Linie + "Jetzt"-Text
C_PAST      = QtGui.QColor(0, 0, 0, 12)        # Schattierung Vergangenheit
# Fuenf Serien teilen sich das Bild. Die Farbtoene liegen deshalb weit
# auseinander (rund 15°, 125°, 210°, 285° im Farbkreis) statt zweimal
# dieselbe Farbe heller und dunkler zu verwenden: ein dunkleres Rot ueber
# einem helleren Rot und ein dunkleres Blau ueber hellblauen Balken waren
# nebeneinander kaum auseinanderzuhalten.
C_TEMP      = QtGui.QColor("#e4572e")          # Temperatur, roh
C_TEMP_MEAN = QtGui.QColor("#2e7d32")          # 24-h-Mittel (Gruen)
C_RAIN      = QtGui.QColor(124, 176, 226, 190) # Niederschlagsbalken (hell)
C_RAIN_INK  = QtGui.QColor("#0d3b66")          # Niederschlag-Text, kumuliert

# Schriften erst beim ersten Gebrauch anlegen und dann behalten. Nicht auf
# Modulebene konstruiert, weil QFont die Schriftdatenbank anfasst und die
# eine laufende QApplication voraussetzt - der Import muss ohne GUI klappen.
_FONT_CACHE: dict[tuple[int, bool], QtGui.QFont] = {}


def _font(size: int, bold: bool = False) -> QtGui.QFont:
    """Arial in der gewuenschten Groesse; gleiche Instanz bei gleichem
    Schluessel. ``QPainter.setFont`` kopiert, das Objekt bleibt also
    unveraendert und ist zwischen Aufrufen gefahrlos wiederverwendbar."""
    f = _FONT_CACHE.get((size, bold))
    if f is None:
        f = QtGui.QFont("Arial", size)
        f.setBold(bold)
        _FONT_CACHE[(size, bold)] = f
    return f


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


def gleitendes_tagesmittel(
    tdts: list[datetime.datetime],
    werte: list[float],
    halbfenster_h: float = 12.0,
) -> list[float | None]:
    """Zentriertes gleitendes 24-Stunden-Mittel über eine Stunden-Zeitreihe.

    Warum genau 24 Stunden: ein gleitendes Mittel über exakt eine Tageslänge
    hat seine spektrale Nullstelle bei der 24-Stunden-Periode und löscht den
    Tagesgang damit vollständig aus, statt ihn nur zu dämpfen. Übrig bleibt
    der Wetterlagen-Trend (Luftmassenwechsel) – genau das, was die rohe
    Stundenkurve durch ihr Auf und Ab verdeckt.

    Die beiden Randwerte des Fensters gehen nur halb ein (Trapezregel). Ein
    geschlossenes 12-Stunden-Fenster enthält bei Stundenwerten 25 Punkte über
    24 Stunden, und der 25. wiederholt die Phase des ersten – ungewichtet
    bliebe davon ein Rest von 1/25 der Tagesgang-Amplitude stehen (gemessen
    0,2 K bei 5 K Amplitude). Mit halben Randgewichten ist die Summe der
    Gewichte wieder 24 und die Auslöschung exakt.

    Das Fenster wird über die *Zeitstempel* aufgespannt, nicht über
    Listenindizes. Fallen einzelne Stunden aus der Reihe – was die
    Zeitachse dieses Moduls ausdrücklich zulässt –, bleibt es trotzdem
    24 Stunden breit, statt auf 24 Messwerte beliebiger Spreizung
    zusammenzuschrumpfen.

    An den Rändern liefert die Funktion ``None`` statt eines Werts, solange
    das Fenster nicht vollständig in der Reihe liegt. Ein mitschrumpfendes
    Fenster wäre die Alternative, ist hier aber die schlechtere: am
    äußersten Rand entspricht das Mittel dann dem Rohwert, und trifft der
    gerade ein Tagesminimum, kippt die Trendlinie dort steil ab und
    behauptet einen Wettersturz, den es nicht gibt. So ist stattdessen
    jeder gezeichnete Punkt ein echtes 24-Stunden-Mittel und mit jedem
    anderen vergleichbar; der Aufrufer zeichnet die ``None``-Ränder nicht.
    """
    n = len(werte)
    if n != len(tdts) or n < 2:
        return [None] * n

    sekunden = [(t - tdts[0]).total_seconds() for t in tdts]
    spanne_s = sekunden[-1]
    halbfenster_s = halbfenster_h * 3600.0

    # Beide Fenstergrenzen wachsen monoton mit i, deshalb genügen zwei
    # mitlaufende Zeiger und eine laufende Summe (statt je Punkt neu zu
    # summieren).
    ergebnis: list[float | None] = []
    lo = hi = 0          # hi ist exklusiv
    summe = 0.0
    for i in range(n):
        s = sekunden[i]
        unten, oben = s - halbfenster_s, s + halbfenster_s

        # Beide Fenstergrenzen wachsen monoton mit i, deshalb laufen die
        # Zeiger je Punkt nur vorwaerts.
        while hi < n and sekunden[hi] <= oben:
            summe += werte[hi]
            hi += 1
        while lo < hi and sekunden[lo] < unten:
            summe -= werte[lo]
            lo += 1

        if unten < 0 or oben > spanne_s or hi - lo < 2:
            ergebnis.append(None)      # Fenster ragt aus der Reihe heraus
            continue

        # Trapezregel: exakt auf der Fenstergrenze liegende Werte nur halb.
        # Die Toleranz von einer Sekunde faengt Rundung in der Sekunden-
        # Arithmetik ab, ohne bei unregelmaessigen Reihen mitzugreifen.
        gew_summe, gewicht = summe, float(hi - lo)
        for rand in (lo, hi - 1):
            if abs(abs(sekunden[rand] - s) - halbfenster_s) <= 1.0:
                gew_summe -= 0.5 * werte[rand]
                gewicht -= 0.5

        ergebnis.append(gew_summe / gewicht if gewicht > 0 else None)
    return ergebnis


def kumulierter_niederschlag(precip: list[float], ab_index: int = 0) -> list[float]:
    """Aufsummierter Niederschlag ab ``ab_index``; davor Null.

    Getrennt von der Zeichenlogik, damit die Summe testbar bleibt. Vor
    ``ab_index`` bewusst Null statt ``None``: die Linie soll flach auf der
    Grundlinie starten und nicht erst in der Bildmitte einsetzen."""
    summe = 0.0
    ergebnis = []
    for i, wert in enumerate(precip):
        if i >= ab_index:
            summe += max(0.0, wert)
        ergebnis.append(summe)
    return ergebnis


def sichtfenster_indizes(
    tdts: list[datetime.datetime],
    start: datetime.datetime,
    ende: datetime.datetime,
    min_spanne_h: float = 6.0,
) -> tuple[int, int]:
    """Index-Halbbereich ``[i0, i1)`` der Zeitstempel im Fenster ``start..ende``.

    Das Fenster wird auf die Reihe geklemmt und auf ``min_spanne_h`` Stunden
    aufgeweitet: unter etwa sechs Stunden bleiben zu wenige Stuetzstellen
    uebrig, und die Zeitachse braucht mindestens zwei Punkte mit echtem
    Abstand, sonst faellt das Diagramm auf die Index-Verteilung zurueck.
    Liefert immer mindestens zwei Indizes, solange die Reihe zwei hergibt."""
    n = len(tdts)
    if n < 2:
        return 0, n
    if ende < start:
        start, ende = ende, start
    if (ende - start).total_seconds() < min_spanne_h * 3600.0:
        mitte = start + (ende - start) / 2
        halb = datetime.timedelta(hours=min_spanne_h / 2)
        start, ende = mitte - halb, mitte + halb

    i0 = next((i for i, t in enumerate(tdts) if t >= start), n - 1)
    i1 = next((i for i in range(n - 1, -1, -1) if tdts[i] <= ende), 0) + 1
    i0 = max(0, min(i0, n - 2))
    i1 = max(i0 + 2, min(i1, n))
    return i0, i1


def temperaturachse(temp: list[float]) -> tuple[float, float, float, int, int]:
    """Achsengrenzen der Temperaturskala: ``(min, max, schritt, teilstriche,
    nachkommastellen)``.

    Die Grenzen werden auf ein "schoenes" Vielfaches gerundet, damit die
    Beschriftung glatte Zahlen zeigt. Eine sehr flache Kurve wird auf
    mindestens 1 K aufgezogen – sonst blaeht die Achse Messrauschen zu einem
    dramatischen Verlauf auf."""
    t_lo, t_hi = min(temp), max(temp)
    if t_hi - t_lo < 0.5:
        mid = (t_hi + t_lo) / 2.0
        t_lo, t_hi = mid - 0.5, mid + 0.5
    step_t = _schoener_schritt(t_hi - t_lo, 6)
    axis_min = math.floor(t_lo / step_t) * step_t
    axis_max = math.ceil(t_hi / step_t) * step_t
    n_ticks = max(1, int(round((axis_max - axis_min) / step_t)))
    t_dec = 0 if step_t >= 1 else 1
    return axis_min, axis_max, step_t, n_ticks, t_dec


def niederschlagsachse(precip: list[float]) -> tuple[float, int]:
    """Obergrenze und Nachkommastellen der Niederschlagsachse.

    Mindestens 1 mm, damit Nieselregen klein dargestellt wird statt als volle
    Saeulenhoehe. Die Reihe ist Gesamtniederschlag in mm Wasseraequivalent,
    also inklusive Schnee und Schauer – ``rain`` allein liesse Schneetage leer
    aussehen, obwohl die Tagessumme darueber Werte zeigt."""
    r_peak = max(precip)
    if r_peak <= 0:
        axis_max_r = 1.0
    else:
        step_r = _schoener_schritt(r_peak, 4)
        axis_max_r = max(math.ceil(r_peak / step_r) * step_r, 1.0)
    r_dec = 1 if axis_max_r < 5 else 0
    return axis_max_r, r_dec


# Untergrenzen der Beaufort-Stufen in m/s nach WMO-Definition. Index =
# Windstaerke; Stufe 12 hat keine Obergrenze.
_BEAUFORT_MS = (0.0, 0.5, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8,
                24.5, 28.5, 32.7)
# Umrechnung der angebotenen Anzeigeeinheiten in m/s. Schluessel ist das
# Anzeigesymbol, weil nur das bis hierher durchgereicht wird; die vier
# Symbole sind untereinander eindeutig.
_IN_MS_JE = {"km/h": 1 / 3.6, "m/s": 1.0, "mph": 0.44704, "kn": 0.514444}


def beaufort(geschwindigkeit: float | None, symbol: str) -> int | None:
    """Windstaerke in Beaufort zur Geschwindigkeit in ``symbol``-Einheiten.

    Beaufort ist keine Umrechnung, sondern eine Einteilung in zwoelf
    Stufen - deshalb eine Tabelle und keine Formel. ``None`` bei fehlendem
    Wert oder unbekannter Einheit: dann bleibt die Stufe einfach weg,
    statt eine falsche Zahl zu nennen."""
    faktor = _IN_MS_JE.get(symbol)
    if geschwindigkeit is None or faktor is None:
        return None
    try:
        ms = float(geschwindigkeit) * faktor
    except (TypeError, ValueError):
        return None
    if ms < 0:
        return None
    stufe = 0
    for i, grenze in enumerate(_BEAUFORT_MS):
        if ms >= grenze:
            stufe = i
    return stufe


def kopfzeile_teile(extras: dict | None, u: dict, lang: str) -> list[str]:
    """Teilstuecke der Bedingungszeile im Kopf (Wetterlage, gefuehlte
    Temperatur, Wind, Feuchte, Tageswerte).

    Jedes Teilstueck erscheint nur, wenn der Wert vorliegt; ohne ``extras``
    bleibt die Liste leer und der Aufrufer laesst die Zeile ganz weg."""
    if not extras:
        return []
    parts = []
    desc = weather_code_text(extras.get("code"), lang)
    if desc:
        parts.append(desc)
    if extras.get("apparent") is not None:
        parts.append(tr("cond_feels", lang,
                        t=_fmt_num(extras["apparent"], 0, lang), u=u["temp"]))
    if extras.get("wind") is not None:
        # Neben der Geschwindigkeit die Beaufort-Stufe: sie ordnet den Wert
        # ein ("wie stark ist das?"), wofuer man km/h erst uebersetzen
        # muesste. Faellt weg, wenn die Einheit nicht bekannt ist.
        bft = beaufort(extras["wind"], u["wind"])
        schluessel = "cond_wind" if bft is None else "cond_wind_bft"
        parts.append(tr(schluessel, lang,
                        v=_fmt_num(extras["wind"], 0, lang), u=u["wind"],
                        b=bft if bft is not None else ""))
    if extras.get("humidity") is not None:
        parts.append(tr("cond_humidity", lang, h=_fmt_num(extras["humidity"], 0, lang)))
    if extras.get("today_min") is not None and extras.get("today_max") is not None:
        parts.append(tr("cond_today", lang,
                        lo=_fmt_num(extras["today_min"], 0, lang),
                        hi=_fmt_num(extras["today_max"], 0, lang),
                        tu=u["temp"],
                        p=_fmt_num(extras.get("today_precip") or 0.0, 1, lang),
                        u=u["precip"]))
    return parts


def tageswerte_tabelle(extras: dict | None) -> dict[str, tuple]:
    """Nachschlagetabelle ``ISO-Datum -> (tmin, tmax, psum)`` aus dem
    ``daily``-Block der API.

    Bewusst nicht aus den Stundenwerten aggregiert: die Kopfzeile liest
    bereits aus ``daily``, und zwei Quellen koennten fuer denselben Tag zwei
    verschiedene Zahlen ins selbe Bild schreiben. Fehlende Teillisten
    ergeben ``None`` an der jeweiligen Stelle, nicht einen Indexfehler."""
    tageswerte = {}
    d = (extras or {}).get("daily") or {}

    def wert(spalte, i):
        """i-ter Wert einer Spalte; fehlt die Spalte oder ist sie kuerzer,
        None statt IndexError."""
        werte = d.get(spalte) or []
        return werte[i] if i < len(werte) else None

    for i, tag in enumerate(d.get("time") or []):
        tageswerte[tag] = (wert("tmin", i), wert("tmax", i), wert("psum", i))
    return tageswerte


# Moegliche Abstaende des Stundenrasters. Nur Teiler von 24, damit das Raster
# an jeder Mitternacht wieder aufgeht und nicht ueber den Tag hinweg wandert.
_STUNDEN_SCHRITTE = (1, 2, 3, 6, 12)
# Ab dieser Tagesbreite werden die Rasterlinien beschriftet. Darunter liegt
# die ungezoomte Ansicht (neun Tage auf 1116 px sind rund 124 px je Tag):
# dort bleibt die einzelne, unbeschriftete Mittagslinie wie bisher stehen.
_PX_PRO_TAG_MIT_UHRZEIT = 180.0
# Mindestabstand zweier Uhrzeiten. Darunter beruehren sich "12:00" und
# "18:00" bei 9 pt.
_PX_JE_UHRZEIT = 52.0
# Unter dieser Tagesbreite stuenden Mitternachts- und Mittagslinie zu dicht
# beieinander; dann ganz ohne Zwischenraster.
_PX_PRO_TAG_MIT_RASTER = 40.0


def tag_wird_zusammengefasst(tag_mitternacht: datetime.datetime,
                             tdts: list[datetime.datetime]) -> bool:
    """Bekommt dieser Tag einen Tagesstreifen (Spanne und Tagessumme)?

    Massgeblich ist die Tagesmitte, nicht die Mitternacht. Ein Ausschnitt
    beginnt selten um 00:00; haenge man den Streifen an die Mitternachtslinie,
    bliebe der angeschnittene erste Tag ohne Summe und die gezeigten
    Tagessummen ergaeben weniger als die kumulierte Linie. Am rechten Rand
    wirkt dieselbe Regel andersherum und laesst den Streifen eines Tages weg,
    der nur noch mit wenigen Stunden im Bild ist."""
    if not tdts:
        return False
    mitte = tag_mitternacht + datetime.timedelta(hours=12)
    return tdts[0] <= mitte <= tdts[-1]


def stunden_schritt(px_pro_tag: float) -> int | None:
    """Abstand des senkrechten Stundenrasters in Stunden; ``None`` = keins.

    Je weiter der Anwender in einen Tag hineinzoomt, desto feiner das Raster:
    Mittagslinie, dann alle sechs, drei, zwei, schliesslich jede Stunde. Der
    Schritt richtet sich nach dem Platz fuer die Beschriftung, nicht nach der
    Zoomstufe selbst – so bleibt der Abstand zweier Uhrzeiten im Bild
    ungefaehr gleich, egal wie breit das Fenster ist."""
    if px_pro_tag < _PX_PRO_TAG_MIT_RASTER:
        return None
    if px_pro_tag < _PX_PRO_TAG_MIT_UHRZEIT:
        return 12
    # Der letzte Eintrag (12 h) erfuellt die Bedingung ab 104 px je Tag
    # immer, und hierher kommt nur, wer ueber 180 px liegt - die Schleife
    # kehrt also stets zurueck.
    for schritt in _STUNDEN_SCHRITTE:
        if px_pro_tag * schritt / 24.0 >= _PX_JE_UHRZEIT:
            return schritt
    return 12




def _stift_fein() -> QtGui.QPen:
    """Stift fuer das unbeschriftete Zwischengitter (waagerecht wie
    senkrecht): feine Punktreihe, 1 px an / 3 px aus. Die Punktierung
    ordnet diese Linien den durchgezogenen Hauptlinien optisch unter,
    ohne sie so hell zu machen, dass man sie nicht mehr ablesen kann."""
    p = QtGui.QPen(C_GRID_FEIN, 1)
    p.setDashPattern([1, 3])
    return p


def _glatter_pfad(punkte: list) -> QtGui.QPainterPath:
    """Catmull-Rom-Spline durch die Punkte, als kubische Bezier-Kurve.

    Tension 1/6 – dieselbe Glaettung fuer Rohkurve und Trendlinie, damit
    beide dieselbe Handschrift haben."""
    path = QtGui.QPainterPath()
    if not punkte:
        return path
    path.moveTo(punkte[0])
    m = len(punkte)
    for j in range(m - 1):
        p0 = punkte[j - 1] if j > 0 else punkte[j]
        p1 = punkte[j]
        p2 = punkte[j + 1]
        p3 = punkte[j + 2] if j + 2 < m else punkte[j + 1]
        c1 = QtCore.QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                            p1.y() + (p2.y() - p0.y()) / 6.0)
        c2 = QtCore.QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                            p2.y() - (p3.y() - p1.y()) / 6.0)
        path.cubicTo(c1, c2, p2)
    return path


class _ChartMaler:
    """Zeichnet ein Wetterdiagramm auf einen vorbereiteten ``QPainter``.

    Ein Objekt je Bild. ``zeichne()`` ruft die Methoden in Zeichenreihenfolge
    auf – was spaeter kommt, liegt oben. Zwischenergebnisse, die mehrere
    Bloecke brauchen (Pixelspalten der Messwerte, Endwert der kumulierten
    Linie, ob eine Serie ueberhaupt sichtbar ist), stehen in Attributen;
    vorher waren es die lokalen Variablen einer einzigen langen Funktion.

    Die Daten kommen fertig geschnitten herein: Sichtfenster, gleitendes
    Mittel und kumulierte Summe berechnet ``render_chart`` auf der vollen
    Reihe, bevor es diese Klasse baut."""

    def __init__(self, painter: QtGui.QPainter, *,
                 times: list[str],
                 temp: list[float],
                 precip: list[float],
                 tdts: list[datetime.datetime],
                 temp_mittel: list[float | None],
                 kum_werte: list[float] | None,
                 lat: float, lon: float,
                 lang: str, u: dict,
                 place: str | None,
                 extras: dict | None,
                 now_local: datetime.datetime | None,
                 view: tuple | None,
                 mittel_an: bool = True,
                 kumuliert_an: bool = True,
                 untergrund: QtGui.QColor | None = None):
        self.p = painter
        self.times, self.temp, self.precip, self.tdts = times, temp, precip, tdts
        self.temp_mittel, self.kum_werte = temp_mittel, kum_werte
        self.lat, self.lon = lat, lon
        self.lang, self.u = lang, u
        self.place, self.extras = place, extras
        self.now_local, self.view = now_local, view
        self.mittel_an, self.kumuliert_an = mittel_an, kumuliert_an
        # SVG und PDF haben keine Grundfarbe - dort muss der Untergrund
        # mitgezeichnet werden, sonst liegt das Diagramm auf Durchsicht.
        # Beim QImage faerbt der Aufrufer vorher per fill(), dann bleibt
        # dies None und es wird nichts zusaetzlich gemalt.
        self.untergrund = untergrund
        self.n = len(temp)

        # --- Abstände (Ränder) ---
        self.width, self.height = LOGICAL_W, LOGICAL_H
        self.margin_left, self.margin_right = MARGIN_LEFT, MARGIN_RIGHT
        self.margin_top, self.margin_bottom = MARGIN_TOP, MARGIN_BOTTOM
        self.plot_w = self.width - self.margin_left - self.margin_right
        self.plot_h = self.height - self.margin_top - self.margin_bottom
        self.plot_bottom = self.height - self.margin_bottom

        (self.axis_min, self.axis_max, self.step_t,
         self.n_ticks, self.t_dec) = temperaturachse(temp)
        self.axis_max_r, self.r_dec = niederschlagsachse(precip)
        self._x_achse_aufbauen()

        # Werden erst beim Zeichnen der jeweiligen Serie bestimmt, aber von
        # spaeteren Bloecken (Endwert-Label, Legende) gelesen. Vorbelegt,
        # damit die Reihenfolge nicht stillschweigend zur Pflicht wird.
        self.zeige_mittel = False
        self.zeige_kumuliert = False
        self.kum_gesamt = 0.0

    # --- Geometrie ---------------------------------------------------------

    def _x_achse_aufbauen(self) -> None:
        """Pixelspalte je Messwert festlegen.

        Bevorzugt aus den echten Zeitstempeln (zeit-proportional). So werden
        herausgefilterte Lücken zeitlich korrekt auseinandergezogen, statt sie
        über den Listenindex zusammenzuschieben. Fällt auf die index-basierte
        Verteilung zurück, falls Zeitstempel fehlen/entartet."""
        n, tdts = self.n, self.tdts
        self.use_time_axis = len(tdts) == n and n >= 2 and tdts[-1] > tdts[0]
        if self.use_time_axis:
            self._t0 = tdts[0]
            self._total_s = (tdts[-1] - self._t0).total_seconds()
            self.xs = [self.x_zeit(td) for td in tdts]
        else:
            self.xs = [self.margin_left + i * self.plot_w / max(n - 1, 1)
                       for i in range(n)]
        self.now_x = self._jetzt_x()

    def x_zeit(self, t: datetime.datetime):
        """Pixelspalte eines Zeitpunkts; ``None`` ohne verlaessliche Zeitachse."""
        if not self.use_time_axis:
            return None
        return (self.margin_left
                + (t - self._t0).total_seconds() / self._total_s * self.plot_w)

    def _jetzt_x(self):
        """Pixel-X des aktuellen Zeitpunkts ermitteln. Mit Zeitachse direkt aus
        der Zeit; sonst robust über Zeitstempel-Interpolation auf die xs."""
        tdts, now_local = self.tdts, self.now_local
        if now_local is None or not tdts:
            return None
        if not (tdts[0] <= now_local <= tdts[-1]):
            return None
        if self.use_time_axis:
            return self.x_zeit(now_local)
        for k in range(len(tdts) - 1):
            if tdts[k] <= now_local <= tdts[k + 1]:
                span = (tdts[k + 1] - tdts[k]).total_seconds() or 1.0
                frac = (now_local - tdts[k]).total_seconds() / span
                return self.xs[k] + frac * (self.xs[k + 1] - self.xs[k])
        return None

    def yt(self, v: float) -> float:
        """Temperaturwert in eine Pixelzeile umrechnen (linke Achse)."""
        return (self.plot_bottom
                - (v - self.axis_min) / (self.axis_max - self.axis_min) * self.plot_h)

    def yr(self, v: float) -> float:
        """Niederschlagswert in eine Pixelzeile umrechnen (rechte Achse)."""
        return self.plot_bottom - (v / self.axis_max_r) * self.plot_h

    def set_font(self, size: int, bold: bool = False) -> None:
        self.p.setFont(_font(size, bold))

    # --- Ablauf ------------------------------------------------------------

    def zeichne(self) -> None:
        """Alle Bloecke in Zeichenreihenfolge; was spaeter kommt, liegt oben."""
        if self.untergrund is not None:
            self.p.fillRect(0, 0, self.width, self.height, self.untergrund)
        self.kopf()
        self.vergangenheit()
        self.gitter()
        self.tageslinien()
        self.achsenlinien()

        # Datenbereich beschneiden, damit Bézier-Überschwinger/Balken nicht
        # über die Achsen hinausragen.
        self.p.save()
        self.p.setClipRect(self.margin_left, self.margin_top,
                           self.plot_w, self.plot_h)
        self.niederschlagsbalken()
        self.temperaturkurve()
        self.tagesmittel()
        self.kumuliert()
        self.jetzt_linie()
        self.p.restore()

        self.kum_endwert()
        self.jetzt_beschriftung()
        self.achsentitel()
        self.legende()
        self.quellen()

    # --- Kopfbereich -------------------------------------------------------

    def kopf(self) -> None:
        """Ort und Ist-Temperatur zuerst, Koordinaten klein.

        Der erste Blick soll den Zustand am Punkt zeigen, nicht die Geometrie.
        Reihenfolge deshalb: Ort gross, Ist-Wert als Hero-Zahl, dann die
        Bedingungszeile, ganz unten Breite/Laenge."""
        p, width, lang, u = self.p, self.width, self.lang, self.u

        self.set_font(10)
        p.setPen(QtGui.QPen(C_MUTE))
        p.drawText(
            QtCore.QRect(0, 8, width, 16),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            tr("chart_title", lang)
        )

        self.set_font(16, bold=True)
        p.setPen(QtGui.QPen(C_INK))
        p.drawText(
            QtCore.QRect(0, 26, width, 24),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            self.place or tr("place_unknown", lang)
        )

        # Ist-Temperatur. Sie wird von der API laengst geliefert, stand aber
        # bisher nirgends im Bild – nur die gefuehlte Temperatur.
        ist_temp = (self.extras or {}).get("temp")
        if ist_temp is not None:
            self.set_font(30, bold=True)
            p.setPen(QtGui.QPen(C_TEMP))
            p.drawText(
                QtCore.QRect(0, 48, width, 44),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                f"{_fmt_num(ist_temp, 1, lang)} {u['temp']}"
            )

        # Aktuelle Bedingungen + heutige Tageswerte in einer kompakten Zeile.
        # Fehlt alles (extras leer/None), bleibt die Zeile weg und das Layout
        # ist unverändert.
        parts = kopfzeile_teile(self.extras, u, lang)
        if parts:
            self._zeile_passend(_TRENNER.join(parts), (10, 9, 8), 94, 15, C_INK)

        # Bei gezoomter Ansicht den sichtbaren Bereich nennen: ein exportiertes
        # PNG waere sonst ohne Kontext, weil die Achse nur Tage beschriftet.
        # Als weiteres Teilstueck DERSELBEN Zeile, nicht als zweite darunter -
        # dafuer ist zwischen Koordinaten und Plotrand kein Platz, und eine
        # zweite Zeile lief in beide hinein.
        unterzeile = [tr("coords", lang, lat=_fmt_num(self.lat, 4, lang),
                         lon=_fmt_num(self.lon, 4, lang))]
        if self.view is not None and self.tdts:
            unterzeile.append(tr("view_range", lang,
                                 von=_format_datetime_label(self.tdts[0], lang),
                                 bis=_format_datetime_label(self.tdts[-1], lang)))
        self._zeile_passend(_TRENNER.join(unterzeile), (9, 8, 7), 111, 14, C_MUTE)

    def _zeile_passend(self, text, groessen, y, hoehe, farbe) -> None:
        """Eine mittige Kopfzeile so setzen, dass sie ins Bild passt.

        Die Kopfzeilen wachsen mit ihrem Inhalt: Wetterlage, gefuehlte
        Temperatur, Wind mit Beaufort-Stufe, Feuchte und Tageswerte ergeben
        im Extremfall rund 1460 px auf 1280 px Bildbreite - der Text liefe
        links und rechts aus dem Bild. Deshalb erst die groesste Schrift
        nehmen, die passt, und nur wenn selbst die kleinste zu breit ist,
        hinten kuerzen. Das ist besser als hart abzuschneiden: ein
        Auslassungszeichen zeigt, dass etwas fehlt."""
        p = self.p
        verfuegbar = self.width - 24
        for groesse in groessen:
            self.set_font(groesse)
            masse = QtGui.QFontMetrics(p.font())
            if masse.horizontalAdvance(text) <= verfuegbar:
                break
        else:
            text = masse.elidedText(
                text, QtCore.Qt.TextElideMode.ElideRight, verfuegbar)
        p.setPen(QtGui.QPen(farbe))
        p.drawText(QtCore.QRect(0, y, self.width, hoehe),
                   QtCore.Qt.AlignmentFlag.AlignCenter, text)

    # --- Hintergrund und Gitter -------------------------------------------

    def vergangenheit(self) -> None:
        """Vergangenheit dezent schattieren (links der "Jetzt"-Linie)."""
        p = self.p
        if self.now_x is not None:
            p.fillRect(
                int(self.margin_left), int(self.margin_top),
                int(self.now_x - self.margin_left), int(self.plot_h),
                C_PAST
            )
        elif self.now_local is not None and self.tdts and self.tdts[-1] < self.now_local:
            # Ganz in der Vergangenheit: es gibt keine Jetzt-Linie im Bild,
            # aber der gesamte Ausschnitt ist vergangen. Ohne diesen Zweig
            # saehe ein zurueckgezoomtes Fenster wie reine Vorhersage aus.
            p.fillRect(int(self.margin_left), int(self.margin_top),
                       int(self.plot_w), int(self.plot_h), C_PAST)

    def gitter(self) -> None:
        """Horizontale Gitterlinien, Y-Achsen-Beschriftung, Null-Grad-Linie.

        Feines, unbeschriftetes Zwischengitter zuerst, damit die beschrifteten
        Hauptlinien darueber liegen. Die Unterteilung haelt die Zwischenwerte
        auf runden Zahlen (Schritt 5 -> je 1 Grad, Schritt 2 -> je 1 Grad),
        damit der Anwender zwischen zwei Beschriftungen ablesen kann."""
        p, ml, pw, pb, ph = (self.p, self.margin_left, self.plot_w,
                             self.plot_bottom, self.plot_h)
        n_ticks, lang = self.n_ticks, self.lang

        mantisse = round(self.step_t / (10 ** math.floor(math.log10(self.step_t))))
        minor_div = 5 if mantisse in (5, 10) else 2
        minor_abstand = ph / n_ticks / minor_div
        # Unter etwa 8 px verschmelzen die Linien optisch zu einer Flaeche –
        # dann lieber ganz weglassen als ein graues Raster zu erzeugen.
        if minor_abstand >= 8:
            p.setPen(_stift_fein())
            for i in range(n_ticks * minor_div + 1):
                if i % minor_div == 0:
                    continue               # liegt auf einer Hauptlinie
                y_pos = int(pb - i * minor_abstand)
                p.drawLine(ml, y_pos, ml + pw, y_pos)

        for i in range(n_ticks + 1):
            frac  = i / n_ticks
            y_pos = int(pb - frac * ph)

            p.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.PenStyle.SolidLine))
            p.drawLine(ml, y_pos, ml + pw, y_pos)

            # Beschriftung links (Temperatur) – Dezimaltrenner nach Sprache
            t_val = self.axis_min + i * self.step_t
            self.set_font(10)
            p.setPen(QtGui.QPen(C_TEMP, 1))
            p.drawText(
                QtCore.QRect(0, y_pos - 9, ml - 8, 18),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"{_fmt_num(t_val, self.t_dec, lang)} {self.u['temp']}"
            )

            # Beschriftung rechts (Niederschlag). Mit Einheit wie die linke
            # Achse ihr Gradzeichen traegt: der Achsentitel steht am Bildrand
            # und faellt beim Ablesen eines Wertes nicht mit ins Auge.
            r_val = frac * self.axis_max_r
            p.setPen(QtGui.QPen(C_RAIN_INK, 1))
            p.drawText(
                QtCore.QRect(ml + pw + 8, y_pos - 9, self.margin_right - 10, 18),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"{_fmt_num(r_val, self.r_dec, lang)} {self.u['precip']}"
            )

        # Null-Grad-Linie, nur wenn die Achse den Gefrierpunkt kreuzt: die
        # Grenze zwischen Regen und Schnee, Nass und Glatteis. Ausserhalb
        # eines Frostzeitraums waere sie nur eine weitere Linie, deshalb die
        # Bedingung statt dauerhafter Anzeige.
        if self.axis_min < 0 < self.axis_max:
            y_null = int(self.yt(0.0))
            p.setPen(QtGui.QPen(C_FROST, 1.4, QtCore.Qt.PenStyle.SolidLine))
            p.drawLine(ml, y_null, ml + pw, y_null)

    # --- Zeitachse ---------------------------------------------------------

    def _stundenlinie(self, x_stunde, stift) -> None:
        """Linie des Zwischenrasters innerhalb eines Tages (12:00 Ortszeit
        und, weiter hineingezoomt, feiner), damit sich die Tageszeit
        abschaetzen laesst."""
        self.p.setPen(stift)
        self.p.drawLine(int(x_stunde), self.margin_top,
                        int(x_stunde), self.plot_bottom)

    def _uhrzeit(self, x_stunde, zeitpunkt, abstand_zu_mitternacht,
                 datum_breite, masse) -> None:
        """Uhrzeit unter die Rasterlinie schreiben, in dieselbe Zeile wie
        das Datum.

        Feste 24-Stunden-Schreibweise wie in der Bereichsangabe im Kopf –
        eine 12-Stunden-Form waere breiter und liefe frueher ineinander.
        Zu dicht an einem Datum wird die Uhrzeit weggelassen: beide stehen
        in derselben Zeile, und das Datum hat Vorrang."""
        text = zeitpunkt.strftime("%H:%M")
        self.set_font(9)
        breite = masse.horizontalAdvance(text)
        if abstand_zu_mitternacht < (datum_breite + breite) / 2.0 + 6:
            self.set_font(10)
            return
        self.p.setPen(QtGui.QPen(C_MUTE, 1))
        self.p.drawText(
            QtCore.QRect(int(x_stunde) - 48, self.plot_bottom + 7, 96, 16),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            text,
        )
        self.set_font(10)   # Datumszeile zeichnet mit 10 pt weiter

    def _tagesstreifen(self, x_mitte, tag_iso, tageswerte) -> None:
        """Zwei kurze Zeilen unter dem Datum: Spanne und Tagessumme.

        Ohne Einheiten – die tragen bereits beide Achsen. Farbig
        codiert statt beschriftet, damit die Zuordnung ohne Legende
        klar ist."""
        werte = tageswerte.get(tag_iso)
        if werte is None:
            return
        tmin, tmax, psum = werte
        lang, u = self.lang, self.u
        self.set_font(9)
        if tmin is not None and tmax is not None:
            self.p.setPen(QtGui.QPen(C_TEMP, 1))
            self.p.drawText(
                QtCore.QRect(int(x_mitte) - 48, self.plot_bottom + 22, 96, 14),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                tr("strip_temp", lang, lo=_fmt_num(tmin, 0, lang),
                   hi=_fmt_num(tmax, 0, lang), u=u["temp"]),
            )
        if psum is not None:
            self.p.setPen(QtGui.QPen(C_RAIN_INK, 1))
            self.p.drawText(
                QtCore.QRect(int(x_mitte) - 48, self.plot_bottom + 37, 96, 14),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                tr("strip_precip", lang, p=_fmt_num(psum, 1, lang), u=u["precip"]),
            )

    def tageslinien(self) -> None:
        """Senkrechte Tages- und Stundenlinien samt Datum, Uhrzeit und
        Tagesstreifen."""
        p, ml, pw, pb, mt = (self.p, self.margin_left, self.plot_w,
                             self.plot_bottom, self.margin_top)
        tdts, n, lang = self.tdts, self.n, self.lang
        self.set_font(10)

        # Tagesbreite in Pixeln, einmal fuer beide Zweige. Getrennt gerechnet
        # stimmten die zwei Formeln nur bei exakt stuendlichen Reihen ueberein.
        if self.use_time_axis:
            px_pro_tag = pw * 86400.0 / self._total_s
        else:
            px_pro_tag = pw * 24.0 / max(n - 1, 1)
        schritt_h = stunden_schritt(px_pro_tag)
        # Uhrzeiten erst ab der Zoomstufe, ab der sie lesbar nebeneinander
        # passen. Ungezoomt bleibt es bei der unbeschrifteten Mittagslinie.
        zeige_uhrzeit = (schritt_h is not None
                         and px_pro_tag >= _PX_PRO_TAG_MIT_UHRZEIT)
        stift_fein = _stift_fein()   # immer derselbe, nicht je Linie neu
        # Schriftmasse einmal statt je Beschriftung: tief gezoomt stehen bis
        # zu 23 Uhrzeiten pro Tag im Bild, und gezoomt wird mit 60 Rendern
        # pro Sekunde.
        masse_uhrzeit = QtGui.QFontMetrics(_font(9))
        masse_datum = QtGui.QFontMetrics(_font(10))

        tageswerte = tageswerte_tabelle(self.extras)
        # Unter etwa 60 px Tagesbreite ueberlappen die beiden Zeilen.
        zeige_streifen = bool(tageswerte) and px_pro_tag >= 60

        if self.use_time_axis:
            # Eine Linie an jedem lokalen Mitternacht – zeitlich korrekt, auch
            # wenn einzelne Stunden herausgefiltert wurden.
            day = tdts[0].replace(hour=0, minute=0, second=0, microsecond=0)
            while day <= tdts[-1]:
                # Breite des Datums dieses Tages, damit eine benachbarte
                # Uhrzeit ihr ausweichen kann. Gemessen statt geschaetzt:
                # "Mi 10.12." ist breiter als "Mo 1.6.".
                self.set_font(10)
                datum_breite = masse_datum.horizontalAdvance(
                    _format_date_label(day, lang))

                for h in range(schritt_h or 24, 24, schritt_h or 24):
                    stunde = day + datetime.timedelta(hours=h)
                    if not (tdts[0] <= stunde <= tdts[-1]):
                        continue
                    x_stunde = self.x_zeit(stunde)
                    self._stundenlinie(x_stunde, stift_fein)
                    if zeige_uhrzeit:
                        self._uhrzeit(x_stunde, stunde,
                                      min(h, 24 - h) * px_pro_tag / 24.0,
                                      datum_breite, masse_uhrzeit)

                if day >= tdts[0]:
                    x_pos = int(self.x_zeit(day))

                    p.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.PenStyle.SolidLine))
                    p.drawLine(x_pos, mt, x_pos, pb)

                    p.setPen(QtGui.QPen(C_AXIS, 1))
                    p.drawLine(x_pos, pb, x_pos, pb + 5)

                    p.setPen(QtGui.QPen(C_MUTE, 1))
                    p.drawText(
                        QtCore.QRect(x_pos - 48, pb + 7, 96, 16),
                        QtCore.Qt.AlignmentFlag.AlignCenter,
                        _format_date_label(day, lang)
                    )

                # Der Streifen gehoert zum Tag, nicht zur Mitternachtslinie:
                # deshalb auf die Tagesmitte, also eine halbe Tagesbreite
                # weiter rechts als das Datum.
                #
                # Ob der Tag zusammengefasst wird, entscheidet seine Mitte
                # (siehe tag_wird_zusammengefasst). Vorher haftete der
                # Streifen an der Mitternachtslinie: der angeschnittene erste
                # Tag bekam keinen, und die gezeigten Tagessummen ergaben
                # weniger als die kumulierte Linie - gemessen 1,3 + 0,0 +
                # 0,6 + 0,0 = 1,9 mm gegen Sigma 3,0 mm, weil die 1,1 mm des
                # angeschnittenen Tages nirgends standen.
                if zeige_streifen and tag_wird_zusammengefasst(day, tdts):
                    self._tagesstreifen(int(self.x_zeit(day)) + px_pro_tag / 2,
                                        day.date().isoformat(), tageswerte)
                    self.set_font(10)   # Datumszeile des naechsten Tages
                day += datetime.timedelta(days=1)
        else:
            # Rückfall ohne verlässliche Zeitstempel: alle 24 Indizes. Hier
            # gibt es kein belastbares Datum, an dem sich ein Tageswert
            # nachschlagen liesse – deshalb ohne Tagesstreifen.
            for i in range(0, n, 24):
                if schritt_h is not None and i + 12 < n:
                    self._stundenlinie(self.xs[i + 12], stift_fein)

                x_pos = int(self.xs[i])

                p.setPen(QtGui.QPen(C_GRID, 1, QtCore.Qt.PenStyle.SolidLine))
                p.drawLine(x_pos, mt, x_pos, pb)

                p.setPen(QtGui.QPen(C_AXIS, 1))
                p.drawLine(x_pos, pb, x_pos, pb + 5)

                if i < len(tdts):
                    date_lbl = _format_date_label(tdts[i], lang)
                else:
                    d = self.times[i]
                    date_lbl = f"{d[8:10]}.{d[5:7]}."

                p.setPen(QtGui.QPen(C_MUTE, 1))
                p.drawText(
                    QtCore.QRect(x_pos - 48, pb + 7, 96, 16),
                    QtCore.Qt.AlignmentFlag.AlignCenter,
                    date_lbl
                )

    def achsenlinien(self) -> None:
        """Achsenlinien (links / rechts / unten), kein harter Vollrahmen."""
        p, ml, pw, pb, mt = (self.p, self.margin_left, self.plot_w,
                             self.plot_bottom, self.margin_top)
        p.setPen(QtGui.QPen(C_AXIS, 1))
        p.drawLine(ml, mt, ml, pb)
        p.drawLine(ml + pw, mt, ml + pw, pb)
        p.drawLine(ml, pb, ml + pw, pb)

    # --- Datenserien (innerhalb der Beschneidung) --------------------------

    def niederschlagsbalken(self) -> None:
        """Stundenbalken, zuerst – die Temperaturlinie liegt darüber."""
        p, pb, n = self.p, self.plot_bottom, self.n
        bar_w = max(4, int(self.plot_w / n * 0.7))
        for i in range(n):
            if self.precip[i] > 0:
                bx = int(self.xs[i]) - bar_w // 2
                by = int(self.yr(self.precip[i]))
                p.fillRect(bx, by, bar_w, pb - by, C_RAIN)

    def temperaturkurve(self) -> None:
        """Geglättete Kurve (Catmull-Rom → kubische Bézier) mit Flächenfüllung."""
        p, pb = self.p, self.plot_bottom
        pts = [QtCore.QPointF(self.xs[i], self.yt(self.temp[i]))
               for i in range(self.n)]
        line_path = _glatter_pfad(pts)

        # Flächenfüllung mit Verlauf (oben kräftig → unten transparent),
        # wirkt deutlich weniger "ausgewaschen" als eine flache Pastellfläche.
        fill_path = QtGui.QPainterPath()
        fill_path.moveTo(QtCore.QPointF(pts[0].x(), pb))
        fill_path.lineTo(pts[0])
        fill_path.connectPath(line_path)
        fill_path.lineTo(QtCore.QPointF(pts[-1].x(), pb))
        fill_path.closeSubpath()

        grad = QtGui.QLinearGradient(0.0, float(self.margin_top), 0.0, float(pb))
        grad.setColorAt(0.0, QtGui.QColor(228, 87, 46, 110))
        grad.setColorAt(1.0, QtGui.QColor(228, 87, 46, 0))
        p.fillPath(fill_path, QtGui.QBrush(grad))

        pen_t = QtGui.QPen(C_TEMP, 2.6)
        pen_t.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen_t.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.strokePath(line_path, pen_t)

    def tagesmittel(self) -> None:
        """Gleitendes 24-Stunden-Mittel als Trendlinie darueber.

        Ueber der Rohkurve, nie an ihrer Stelle: der Tagesgang ist ein
        physikalisch echtes Signal und darf nicht weggemittelt verschwinden.
        ``temp_mittel`` steht bereits fest (Aufrufer oder Eigenberechnung auf
        der vollen Reihe) und ist ggf. aufs Sichtfenster geschnitten.

        Nur ueber ``zeichne()`` aufrufen: setzt ``zeige_mittel`` fuer die
        Legende."""
        if not self.mittel_an:
            return
        p, n = self.p, self.n
        temp_mittel = self.temp_mittel if self.use_time_axis else []
        pen_m = QtGui.QPen(C_TEMP_MEAN, 1.8)
        pen_m.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen_m.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)

        # Die Randbereiche ohne vollstaendiges Fenster liefern None. Sie
        # trennen die Reihe in zusammenhaengende Stuecke, die einzeln
        # gezeichnet werden – so entsteht keine Linie quer ueber eine Luecke.
        # Vorab bestimmen statt als Seiteneffekt in der Zeichenschleife zu
        # setzen: die Legende weiter unten braucht die Antwort ebenfalls.
        self.zeige_mittel = sum(1 for v in temp_mittel if v is not None) >= 2
        stueck: list[QtCore.QPointF] = []
        for i in range(n + 1):
            wert = temp_mittel[i] if i < n and i < len(temp_mittel) else None
            if wert is None:
                if len(stueck) >= 2:
                    p.strokePath(_glatter_pfad(stueck), pen_m)
                stueck = []
            else:
                stueck.append(QtCore.QPointF(self.xs[i], self.yt(wert)))

    def kumuliert(self) -> None:
        """Kumulierter Niederschlag ab "jetzt".

        Beantwortet "wieviel kommt noch", was die Stundenbalken nicht zeigen.
        Eigene Skalierung: die Gesamtsumme ist ein Vielfaches des
        Stundenmaximums, auf der Regenachse gezeichnet wuerde sie die Balken
        zu Strichen zusammendruecken.

        Nur ueber ``zeichne()`` aufrufen: setzt ``kum_gesamt`` und
        ``zeige_kumuliert``, die Endwert-Label und Legende lesen."""
        if not self.kumuliert_an:
            return
        p, n, pb = self.p, self.n, self.plot_bottom
        if self.kum_werte is None:
            kum_ab = 0
            if self.now_x is not None:
                kum_ab = next((i for i in range(n) if self.xs[i] >= self.now_x), n)
            self.kum_werte = kumulierter_niederschlag(self.precip, kum_ab)
        kum = self.kum_werte
        # Der Endwert ist die Summe bis zum rechten Rand des Ausschnitts,
        # nicht die Gesamtsumme der Reihe - die Linie endet ja hier.
        self.kum_gesamt = kum[-1] if kum else 0.0

        # Unter einem Zwanzigstel Millimeter ist die Linie eine flache Null und
        # das Endlabel "Σ 0,0" reines Rauschen - an einer trockenen Woche weg.
        self.zeige_kumuliert = self.kum_gesamt >= 0.05
        if not self.zeige_kumuliert:
            return

        kum_pfad = QtGui.QPainterPath()
        kum_pfad.moveTo(QtCore.QPointF(self.xs[0], self._y_kum(kum[0])))
        for i in range(1, n):
            # Treppe statt Bezier: eine Summe springt zur vollen Stunde und
            # steigt nicht weich dazwischen an.
            kum_pfad.lineTo(QtCore.QPointF(self.xs[i], self._y_kum(kum[i - 1])))
            kum_pfad.lineTo(QtCore.QPointF(self.xs[i], self._y_kum(kum[i])))
        pen_k = QtGui.QPen(C_RAIN_INK, 1.4)
        pen_k.setDashPattern(_STRICH_KUMULIERT)
        p.strokePath(kum_pfad, pen_k)

    def _y_kum(self, v: float) -> float:
        """Pixelzeile der kumulierten Linie. Der Endwert landet bei 90 % der
        Plothoehe, damit oben Platz fuer das Endlabel bleibt."""
        oben = self.margin_top + 0.1 * self.plot_h
        return self.plot_bottom - (v / self.kum_gesamt) * (self.plot_bottom - oben)

    def jetzt_linie(self) -> None:
        """"Jetzt"-Linie (gestrichelt, innerhalb des Plots)."""
        if self.now_x is None:
            return
        nx = int(self.now_x)
        self.p.setPen(QtGui.QPen(C_NOW, 1.8, QtCore.Qt.PenStyle.DashLine))
        self.p.drawLine(nx, self.margin_top, nx, self.plot_bottom)

    # --- Beschriftungen ueber dem Plot -------------------------------------

    def kum_endwert(self) -> None:
        """Endwert der kumulierten Linie beschriften.

        Hier mit Einheit: der Wert haengt an keiner der beiden Achsen.

        Das Feld sitzt mittig AUF der Linie und deckt sie auf seiner Breite
        ab. Daneben gesetzt war die Zuordnung unklar - der Wert schwebte
        unter einer Linie, die auch zum Gitter haette gehoeren koennen. Der
        Ruecken ist fast deckend, sonst liefe das Strichmuster durch die
        Ziffern."""
        if not self.zeige_kumuliert:
            return
        p, ml, pw = self.p, self.margin_left, self.plot_w
        self.set_font(9, bold=True)
        txt = tr("cum_total", self.lang,
                 p=_fmt_num(self.kum_gesamt, 1, self.lang), u=self.u["precip"])
        hoehe = 15
        breite = QtGui.QFontMetrics(p.font()).horizontalAdvance(txt) + 12
        x = ml + pw - breite - 4
        # Endhoehe der Linie: dort liegt ihr letzter Punkt, also der Wert,
        # den dieses Feld benennt.
        y = int(self._y_kum(self.kum_gesamt)) - hoehe // 2
        p.fillRect(x, y, breite, hoehe, QtGui.QColor(255, 255, 255, 238))
        p.setPen(QtGui.QPen(C_RAIN_INK))
        p.drawText(QtCore.QRect(x, y, breite, hoehe),
                   QtCore.Qt.AlignmentFlag.AlignCenter, txt)

    def jetzt_beschriftung(self) -> None:
        """"Jetzt"-Beschriftung: parallel zur Linie, von rechts lesbar.

        Frueher stand sie waagerecht oberhalb des Plots und lief dort in die
        Kopfzeile mit den aktuellen Bedingungen. Jetzt laeuft sie um 90 Grad
        gedreht neben der Linie mit (gleiche Leserichtung wie der rechte
        Achsentitel) und bleibt damit innerhalb des Plots."""
        if self.now_x is None:
            return
        p, ml, pw = self.p, self.margin_left, self.plot_w
        nx = int(self.now_x)
        lbl_w, lbl_h, gap = 46, 14, 5

        # Rechts der Linie ist die Vorgabe; am rechten Plotrand kippt die
        # Beschriftung auf die linke Seite, damit sie im Bild bleibt.
        place_right = nx + gap + lbl_h <= ml + pw - 4
        if place_right:
            box_left = nx + gap
        else:
            box_left = nx - gap - lbl_h

        box_top = self.margin_top + 6

        # Dezenter weisser Ruecken, damit der Text ueber der Temperaturkurve
        # lesbar bleibt (gleiche Machart wie bei der Legende).
        p.fillRect(box_left, box_top, lbl_h, lbl_w,
                   QtGui.QColor(255, 255, 255, 215))

        self.set_font(9, bold=True)
        p.setPen(QtGui.QPen(C_NOW))
        p.save()
        # rotate(90) bildet lokal (x, y) auf den Bildschirm-Versatz
        # (-y, x) ab. Mit lokalem y in [-lbl_h, 0] deckt der Text also
        # genau die Bildschirmspalte [box_left, box_left + lbl_h] ab und
        # laeuft dabei von box_top nach unten.
        p.translate(box_left, box_top)
        p.rotate(90)
        p.drawText(
            QtCore.QRect(0, -lbl_h, lbl_w, lbl_h),
            QtCore.Qt.AlignmentFlag.AlignCenter,
            tr("now", self.lang)
        )
        p.restore()

    def achsentitel(self) -> None:
        """Beide Achsentitel, senkrecht an den Raendern.

        Ohne Einheit: die traegt jetzt jeder Teilstrich beider Skalen. Der
        Titel doppelte sie nur, und "0,8 mm" stiess rechts an ein
        "Niederschlag (mm)" direkt daneben."""
        p, ph, mt = self.p, self.plot_h, self.margin_top
        self.set_font(12, bold=True)
        p.setPen(QtGui.QPen(C_TEMP))
        p.save()
        p.translate(20, mt + ph / 2)
        p.rotate(-90)
        p.drawText(QtCore.QRect(int(-ph / 2), -16, int(ph), 30),
                   QtCore.Qt.AlignmentFlag.AlignCenter,
                   tr("temp_axis", self.lang))
        p.restore()

        p.setPen(QtGui.QPen(C_RAIN_INK))
        p.save()
        p.translate(self.width - 18, mt + ph / 2)
        p.rotate(90)
        p.drawText(QtCore.QRect(int(-ph / 2), -16, int(ph), 30),
                   QtCore.Qt.AlignmentFlag.AlignCenter,
                   tr("rain_axis", self.lang))
        p.restore()

    def legende(self) -> None:
        """Legende unten rechts, unterhalb des Diagramms.

        Ausserhalb der Zeichenflaeche: so verdeckt sie keine Kurvenspitze
        und die "Jetzt"-Beschriftung muss ihr nicht mehr ausweichen. Die
        Breite wird gemessen statt geraten – sonst laufen laengere
        Beschriftungen (andere Sprache, dritter Eintrag) heraus."""
        p, ml, pw, pb, lang = (self.p, self.margin_left, self.plot_w,
                               self.plot_bottom, self.lang)
        MARKE_B, MARKE_ABSTAND, EINTRAG_ABSTAND = 24, 6, 18
        # Vierter Eintrag je Zeile ist das Strichmuster. Ohne das zeigte die
        # Legende fuer die kumulierte Linie einen durchgezogenen Strich,
        # waehrend im Bild eine gestrichelte Linie liegt - die Marke muss die
        # Linie wiedergeben, die sie erklaert.
        leg_eintraege = [("linie", C_TEMP, tr("legend_temp", lang), None)]
        if self.zeige_mittel:
            leg_eintraege.append(("linie", C_TEMP_MEAN,
                                  tr("legend_temp_mean", lang), None))
        leg_eintraege.append(("flaeche", C_RAIN, tr("legend_rain", lang), None))
        if self.zeige_kumuliert:
            leg_eintraege.append(("linie", C_RAIN_INK,
                                  tr("legend_cum", lang), _STRICH_KUMULIERT))

        self.set_font(10)
        fm = QtGui.QFontMetrics(p.font())
        leg_breiten = [MARKE_B + MARKE_ABSTAND + fm.horizontalAdvance(txt)
                       for _, _, txt, _s in leg_eintraege]
        leg_w = sum(leg_breiten) + EINTRAG_ABSTAND * (len(leg_breiten) - 1)
        cy = pb + 72                   # unter Datum und Tagesstreifen
        ex = ml + pw - leg_w           # rechtsbuendig zur Plot-Kante

        for (art, farbe, txt, strich), breite in zip(leg_eintraege, leg_breiten):
            if art == "linie":
                # Duennerer Strich als frueher (3 px), sonst verschluckt die
                # Marke ein feines Muster.
                stift = QtGui.QPen(farbe, 2.2)
                if strich:
                    stift.setDashPattern(strich)
                p.setPen(stift)
                p.drawLine(ex, cy, ex + MARKE_B, cy)
            else:
                p.fillRect(ex, cy - 5, MARKE_B, 10, farbe)
            p.setPen(QtGui.QPen(C_INK))
            p.drawText(ex + MARKE_B + MARKE_ABSTAND, cy + 4, txt)
            ex += breite + EINTRAG_ABSTAND

    def quellen(self) -> None:
        """Quellenangabe ins Bild selbst.

        Muss Teil des QImage sein, nicht nur ein Label daneben: Open-Meteo
        steht unter CC BY 4.0, und ein exportiertes oder kopiertes PNG ist
        eine Weitergabe, bei der die Namensnennung mitgehen muss. Die
        i18n-Strings tragen HTML-Links fuer das Fenster-Label – fuer das
        Bild bleibt nur der Text uebrig."""
        p, ml, pw, pb, lang = (self.p, self.margin_left, self.plot_w,
                               self.plot_bottom, self.lang)
        quellen = [_MARKUP.sub("", tr("source", lang))]
        urls = [tr("source_urls", lang)]
        if self.place:
            quellen.append(_MARKUP.sub("", tr("source_osm", lang)))
            urls.append(tr("source_urls_osm", lang))

        self.set_font(8)
        p.setPen(QtGui.QPen(C_MUTE))
        p.drawText(
            QtCore.QRect(ml, pb + 86, pw, 13),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            _TRENNER.join(quellen),
        )
        # Zweite Zeile mit den URIs. _MARKUP entfernt die <a>-Tags samt der
        # href-Adressen – damit stuende im Bild zwar der Name, aber nicht der
        # Link, und CC BY 4.0 verlangt beides. Als eigener i18n-Schluessel
        # statt aus dem HTML zurueckgebaut: kein Parsen, und die Zeile bleibt
        # lesbar statt in geschachtelten Klammern zu enden.
        self.set_font(7)
        p.drawText(
            QtCore.QRect(ml, pb + 99, pw, 12),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            _TRENNER.join(urls),
        )


def zeichendaten(
    lat: float,
    lon: float,
    times: list[str],
    temp: list[float],
    precip: list[float],
    now_local: datetime.datetime | None = None,
    lang: str = "de",
    place: str | None = None,
    extras: dict | None = None,
    units: dict | None = None,
    view: tuple[datetime.datetime, datetime.datetime] | None = None,
    temp_mittel: list[float | None] | None = None,
    kum_werte: list[float] | None = None,
    mittel_an: bool = True,
    kumuliert_an: bool = True,
    tdts: list[datetime.datetime] | None = None,
) -> dict:
    """Rohreihen pruefen, ableiten und aufs Sichtfenster schneiden.

    Liefert genau die Argumente, die ``_ChartMaler`` braucht. Getrennt vom
    Zeichnen, weil dieselbe Aufbereitung fuer jedes Ausgabegeraet gilt -
    Bildschirm, PNG, SVG oder PDF unterscheiden sich erst danach."""

    # Leere Reihen wuerden erst weiter unten in min()/max(), in plot_w / n
    # und in pts[0] knallen. Bisher hing das nur an der Disziplin des einzigen
    # Aufrufers: api.py wirft vorher err_nodata. Diese Kopplung ueber zwei
    # Dateien hier explizit machen, statt sie stillschweigend vorauszusetzen.
    if not temp or not precip:
        raise ValueError("render_chart: temp und precip duerfen nicht leer sein")

    # Anzeige-Einheiten (Symbole); ohne Angabe das bisherige metrische Verhalten.
    u = units or {"temp": "°C", "wind": "km/h", "precip": "mm"}

    # --- Zeitstempel parsen (für Zeitachse, "Jetzt"-Linie, Tageslinien) ---
    # Der Aufrufer darf die geparste Reihe mitgeben. Beim Zoomen laeuft
    # render_chart je Rad-Schritt erneut, und die volle Reihe umfasst rund
    # 216 Stundenwerte - die jedesmal neu aus Text zu parsen ist Arbeit fuer
    # ein Ergebnis, das sich zwischen zwei Rendern nie aendert. Gleiches
    # Muster wie bei temp_mittel und kum_werte.
    if tdts is None:
        try:
            tdts = [datetime.datetime.fromisoformat(s) for s in times]
        except (ValueError, TypeError):
            tdts = []

    # Gleitendes Mittel und kumulierte Summe stammen vom Aufrufer und sind
    # auf der VOLLEN Reihe gerechnet; hier wird nur noch mitgeschnitten.
    # Erst schneiden und dann rechnen waere bei beiden falsch: das Mittel
    # verloere an jedem Zoomrand zwoelf Stunden, und die Summe "ab jetzt"
    # begaenne am Ausschnittsanfang - gemessen fehlten bei einem
    # Ausschnitt 40 Stunden nach jetzt 40 von 91 mm. Ohne Angabe wird wie
    # bisher hier gerechnet, damit der ungezoomte Aufruf gleich bleibt.
    voll_zeitachse = (len(tdts) == len(temp) and len(temp) >= 2
                      and tdts[-1] > tdts[0])
    if temp_mittel is None:
        temp_mittel = (gleitendes_tagesmittel(tdts, temp)
                       if voll_zeitachse else [None] * len(temp))
    if kum_werte is None:
        # Aus demselben Grund wie beim Mittel VOR dem Schnitt: die Summe
        # laeuft ab "jetzt", und jetzt liegt oft links vom Ausschnitt. Erst
        # schneiden und dann summieren verliert alles dazwischen - genau der
        # Fehler, der im Fenster schon behoben ist und hier fuer jeden
        # anderen Aufrufer (Export, Batch) noch bestand.
        ab = 0
        if voll_zeitachse and now_local is not None:
            ab = next((i for i, td in enumerate(tdts) if td >= now_local),
                      len(temp))
        kum_werte = kumulierter_niederschlag(precip, ab)

    # --- Sichtfenster anwenden ---
    if view is not None and len(tdts) == len(temp):
        i0, i1 = sichtfenster_indizes(tdts, view[0], view[1])
        times = times[i0:i1]
        temp = temp[i0:i1]
        precip = precip[i0:i1]
        tdts = tdts[i0:i1]
        temp_mittel = temp_mittel[i0:i1]
        if kum_werte is not None:
            kum_werte = kum_werte[i0:i1]

    return dict(
        times=times, temp=temp, precip=precip, tdts=tdts,
        temp_mittel=temp_mittel, kum_werte=kum_werte,
        lat=lat, lon=lon, lang=lang, u=u,
        place=place, extras=extras,
        now_local=now_local, view=view,
        mittel_an=mittel_an, kumuliert_an=kumuliert_an,
    )


def zeichnen_auf(geraet, daten: dict, scale: float = 1.0) -> None:
    """Aufbereitete Daten auf ein beliebiges QPaintDevice zeichnen.

    ``_ChartMaler`` haengt an keinem Bildformat, nur an einem QPainter -
    dasselbe Diagramm geht so auf ein QImage, einen QSvgGenerator oder
    einen QPdfWriter, ohne dass der Zeichencode davon weiss."""
    painter = QtGui.QPainter(geraet)
    # try/finally: bei einer Exception während des Zeichnens muss painter.end()
    # trotzdem laufen, sonst bleibt das Geraet gesperrt (Ressourcen-Leck).
    try:
        painter.scale(scale, scale)  # in logischen Koordinaten zeichnen
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        _ChartMaler(painter, **daten).zeichne()
    finally:
        painter.end()


def render_chart(lat: float, lon: float, times: list[str], temp: list[float],
                 precip: list[float], now_local: datetime.datetime | None = None,
                 scale: float = 1.0, **kwargs) -> QtGui.QImage:
    """Diagramm als QImage in Geraete-Pixeldichte ``scale``.

    Physisch wird mit ``scale`` gerendert, damit nichts heruntergerechnet
    (= unscharf) werden muss."""
    daten = zeichendaten(lat, lon, times, temp, precip, now_local, **kwargs)
    if scale < 1.0:
        scale = 1.0

    img = QtGui.QImage(
        max(1, int(LOGICAL_W * scale)),
        max(1, int(LOGICAL_H * scale)),
        QtGui.QImage.Format.Format_ARGB32,
    )
    img.fill(QtGui.QColor("white"))
    zeichnen_auf(img, daten, scale)
    # Geräte-Pixeldichte am Bild vermerken, damit es 1:1 (scharf) angezeigt wird.
    img.setDevicePixelRatio(scale)
    return img
