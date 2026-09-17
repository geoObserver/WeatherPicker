"""Weather Picker - Processing-Provider: Wetterabruf als Batch-Werkzeug.

Macht aus dem Klick-Werkzeug einen modellierbaren, batch-faehigen Algorithmus:
ein Punkt-Layer rein, je Punkt ein Wetterabruf, ein Punkt-Layer mit den
Ergebnissen (Felder wie im Sammel-Layer, siehe ``points_layer._build_fields``)
und optional ein zweiter Layer mit einer Zeile je Punkt UND Stunde raus.

Ein fehlgeschlagener Abruf bricht den Lauf nicht ab - er wird gezaehlt und am
Ende gemeldet (``feedback.reportError(..., fatalError=False)``); die Schleife
laeuft mit dem naechsten Punkt weiter.
"""
from __future__ import annotations

import datetime
import os

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingProvider,
    QgsVectorLayer,
    QgsVectorLayerTemporalProperties,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant
from qgis.PyQt.QtGui import QIcon

from . import api, points_layer, settings
from .chart import zeichendaten
from .export import diagramm_ablegen
from .i18n import _current_lang, tr, weather_code_text

# Qt6/QGIS-3.38+: QgsField(name, QVariant.Type) ist deprecated/auf Qt6-Builds
# gefaehrlich - dieselbe Weiche wie in points_layer._build_fields(), damit
# beide Layer (Sammel-Layer und dieser Processing-Output) sich unter derselben
# QGIS-Version gleich verhalten.
if Qgis.QGIS_VERSION_INT >= 33800:
    from qgis.PyQt.QtCore import QMetaType
    _STR, _DBL, _DT = (QMetaType.Type.QString, QMetaType.Type.Double,
                       QMetaType.Type.QDateTime)
else:  # QGIS < 3.38 (Qt5)
    _STR, _DBL, _DT = QVariant.String, QVariant.Double, QVariant.DateTime


# Python-Referenzen auf ausgegebene Post-Processor-Objekte. QGIS übernimmt
# keine Ownership an einem QgsProcessingLayerPostProcessorInterface (siehe
# dessen Doku) - ohne dies würde der GC sie einsammeln, bevor QGIS sie beim
# Laden des Ergebnis-Layers benutzt, und QGIS stürzt beim Zugriff auf die
# tote C++-Seite ab.
_POST_PROCESSORS: list = []


class _StundenwerteZeitPostProcessor(QgsProcessingLayerPostProcessorInterface):
    """Setzt die zeitliche Eigenschaft des Stundenwerte-Layers auf das Feld
    ``time``, damit sich die Vorhersage im QGIS-Temporal-Controller animieren
    lässt."""

    def postProcessLayer(self, layer, context, feedback) -> None:
        if not isinstance(layer, QgsVectorLayer):
            return
        props = layer.temporalProperties()
        props.setMode(QgsVectorLayerTemporalProperties.ModeFeatureDateTimeInstantFromField)
        props.setStartField("time")
        props.setIsActive(True)


class WetterFuerPunkteAlgorithm(QgsProcessingAlgorithm):
    """Wetterabruf je Punkt eines Punkt-Layers - Batch-Fassung des Klick-Werkzeugs."""

    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    OUTPUT_HOURLY = "OUTPUT_HOURLY"
    CHARTS = "CHARTS"
    PLACES = "PLACES"

    def createInstance(self):
        return WetterFuerPunkteAlgorithm()

    def name(self) -> str:
        return "wetter_fuer_punkte"

    def displayName(self) -> str:
        return tr("proc_alg_name", _current_lang())

    def group(self) -> str:
        return "Weather Picker"

    def groupId(self) -> str:
        return "weather_picker"

    def shortHelpString(self) -> str:
        return tr("proc_alg_help", _current_lang())

    # Kein flags()-Override: der Spike (siehe Abschlussbericht) hat
    # QFontMetrics/QPainter/QImage in einem eigenstaendigen QThread erfolgreich
    # gerendert (QGIS 3.44 "Solothurn") - der Algorithmus darf also im
    # QgsTask-Worker-Thread laufen, FlagNoThreading ist nicht noetig.

    def initAlgorithm(self, config=None) -> None:
        lang = _current_lang()

        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("proc_param_input", lang),
            [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("proc_param_output", lang),
            QgsProcessing.TypeVectorPoint))

        self.addParameter(QgsProcessingParameterBoolean(
            self.CHARTS, tr("proc_param_charts", lang), defaultValue=True))

        places_param = QgsProcessingParameterBoolean(
            self.PLACES, tr("proc_param_places", lang), defaultValue=False)
        places_param.setHelp(tr("proc_param_places_help", lang))
        self.addParameter(places_param)

        hourly_param = QgsProcessingParameterFeatureSink(
            self.OUTPUT_HOURLY, tr("proc_param_hourly", lang),
            QgsProcessing.TypeVectorPoint, optional=True)
        self.addParameter(hourly_param)

    @staticmethod
    def _stundenwerte_felder() -> QgsFields:
        """Felder des optionalen Stundenwerte-Sinks: eine Zeile je Punkt und
        Stunde. ``time`` als echtes Datums-/Zeitfeld (nicht String), damit der
        Temporal-Controller darauf animieren kann."""
        fields = QgsFields()
        for name, qtype in (("ort", _STR), ("time", _DT),
                            ("temp", _DBL), ("precip", _DBL)):
            fields.append(QgsField(name, qtype))
        return fields

    def processAlgorithm(self, parameters, context, feedback):
        lang = _current_lang()

        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.INPUT))

        make_charts = self.parameterAsBoolean(parameters, self.CHARTS, context)
        find_places = self.parameterAsBoolean(parameters, self.PLACES, context)

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        out_fields = points_layer._build_fields()
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, QgsWkbTypes.Point, wgs84)
        if sink is None:
            raise QgsProcessingException(
                self.invalidSinkError(parameters, self.OUTPUT))

        # Der zweite Sink ist optional; ohne gewaehltes Ziel liefert
        # parameterAsSink (None, "") und wird schlicht uebersprungen. Die
        # Feld-Definition wird nur einmal gebaut und fuer Sink UND jedes
        # Stundenwerte-Feature wiederverwendet (siehe hourly_fields unten).
        hourly_fields = self._stundenwerte_felder()
        hourly_sink, hourly_dest_id = self.parameterAsSink(
            parameters, self.OUTPUT_HOURLY, context,
            hourly_fields, QgsWkbTypes.Point, wgs84)
        if hourly_sink is None:
            hourly_dest_id = None

        # Eingabegeometrie nach WGS84 transformieren: die API will Grad, das
        # Projekt/der Layer kann in jedem beliebigen CRS vorliegen. Eine
        # ungueltige Quell-CRS wird einmal gemeldet statt stillschweigend
        # Kartenkoordinaten als WGS84-Grad zu verwenden.
        src_crs = source.sourceCrs()
        transform = None
        if not src_crs.isValid():
            feedback.reportError(tr("proc_err_crs", lang), fatalError=False)
        elif src_crs != wgs84:
            transform = QgsCoordinateTransform(src_crs, wgs84, context.transformContext())

        # featureCount() liefert bei manchen Providern -1 (unbekannt) -
        # dann bleibt der Fortschrittsbalken auf 0 statt durch Null zu teilen.
        total = source.featureCount()
        total = total if total and total > 0 else 0

        # Ordner fuer Diagramme einmal ermitteln (legt ihn ggf. an) statt bei
        # jedem Punkt erneut nachzusehen.
        chart_ordner = points_layer.bildordner() if make_charts else None

        fehlgeschlagen = 0
        verarbeitet = 0

        for current, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                fehlgeschlagen += 1
                feedback.reportError(tr("proc_err_no_geom", lang, i=current),
                                     fatalError=False)
                continue

            # Punkt-Layer kann MultiPoint sein (z. B. aus einem Import) - dann
            # zaehlt der erste Teilpunkt. Ein leeres MultiPoint (theoretisch
            # moeglich trotz bestandener isEmpty()-Pruefung oben) zaehlt als
            # Fehlschlag statt den ganzen Lauf per IndexError abzubrechen.
            if geom.isMultipart():
                teilpunkte = geom.asMultiPoint()
                if not teilpunkte:
                    fehlgeschlagen += 1
                    feedback.reportError(tr("proc_err_no_geom", lang, i=current),
                                         fatalError=False)
                    continue
                punkt = teilpunkte[0]
            else:
                punkt = geom.asPoint()

            if transform is not None:
                try:
                    punkt = transform.transform(punkt)
                except Exception as exc:
                    fehlgeschlagen += 1
                    feedback.reportError(
                        tr("proc_err_transform", lang, i=current, msg=str(exc)),
                        fatalError=False)
                    continue

            lat, lon = punkt.y(), punkt.x()

            try:
                times, temp, precip, utc_offset, extras = api.fetch_weather(
                    lat, lon, lang)
            except Exception as exc:
                fehlgeschlagen += 1
                feedback.reportError(
                    tr("proc_err_fetch", lang, i=current, msg=str(exc)),
                    fatalError=False)
                if total:
                    feedback.setProgress((current + 1) / total * 100)
                continue

            # Geocoding ist optional und schlaegt laut eigener Doku still auf
            # None fehl (kein try/except noetig) - wie in weather_picker.py.
            place = api.reverse_geocode(lat, lon, lang) if find_places else None

            now_local = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=utc_offset)
            ).replace(tzinfo=None)

            chart_path = ""
            if make_charts:
                try:
                    daten = zeichendaten(
                        lat, lon, times, temp, precip, now_local=now_local,
                        lang=lang, place=place, extras=extras,
                        units=settings.unit_symbols())
                    chart_path = diagramm_ablegen(chart_ordner, daten, lat, lon)
                except Exception as exc:
                    feedback.reportError(
                        tr("proc_err_chart", lang, i=current, msg=str(exc)),
                        fatalError=False)

            ex = extras or {}
            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
            out_feat["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            out_feat["place"] = place or ""
            out_feat["lon"] = round(float(lon), 6)
            out_feat["lat"] = round(float(lat), 6)
            out_feat["temp"] = ex.get("temp")
            out_feat["feels"] = ex.get("apparent")
            out_feat["humidity"] = ex.get("humidity")
            out_feat["wind"] = ex.get("wind")
            out_feat["t_min"] = ex.get("today_min")
            out_feat["t_max"] = ex.get("today_max")
            out_feat["precip"] = ex.get("today_precip")
            out_feat["weather"] = weather_code_text(ex.get("code"), lang)
            out_feat["chart"] = chart_path
            sink.addFeature(out_feat, QgsFeatureSink.Flag.FastInsert)

            if hourly_sink is not None:
                for t_iso, tp, pr in zip(times, temp, precip):
                    try:
                        dt = datetime.datetime.fromisoformat(t_iso)
                    except (ValueError, TypeError):
                        continue
                    hf = QgsFeature(hourly_fields)
                    hf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
                    hf["ort"] = place or ""
                    hf["time"] = QDateTime(QDate(dt.year, dt.month, dt.day),
                                           QTime(dt.hour, dt.minute, dt.second))
                    hf["temp"] = float(tp)
                    hf["precip"] = float(pr)
                    hourly_sink.addFeature(hf, QgsFeatureSink.Flag.FastInsert)

            verarbeitet += 1
            if total:
                feedback.setProgress((current + 1) / total * 100)

        if fehlgeschlagen:
            feedback.pushInfo(tr(
                "proc_summary_failed", lang,
                n=fehlgeschlagen, total=verarbeitet + fehlgeschlagen))

        results = {self.OUTPUT: dest_id}
        if hourly_dest_id is not None:
            results[self.OUTPUT_HOURLY] = hourly_dest_id
            if context.willLoadLayerOnCompletion(hourly_dest_id):
                processor = _StundenwerteZeitPostProcessor()
                _POST_PROCESSORS.append(processor)
                context.layerToLoadOnCompletionDetails(hourly_dest_id) \
                    .setPostProcessor(processor)
        return results


class WeatherPickerProvider(QgsProcessingProvider):
    """Processing-Provider des Plugins. Registrierung/Deregistrierung macht
    ``weather_picker.py`` selbst (``QgsApplication.processingRegistry()``)."""

    def id(self) -> str:
        return "weather_picker"

    def name(self) -> str:
        return "Weather Picker"

    def icon(self):
        # Gleiches Icon wie die Toolbar-Action (siehe weather_picker.py); mit
        # Ausweich-Icon der Basisklasse, falls logo.png fehlt.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        return QIcon(path) if os.path.exists(path) else QgsProcessingProvider.icon(self)

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(WetterFuerPunkteAlgorithm())
