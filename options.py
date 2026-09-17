"""Weather Picker - Optionsdialog (Endpoint, API-Schluessel, Einheiten).

Liest/schreibt ueber das settings-Modul. Bewusst schlank in Code aufgebaut
(kein .ui-File), damit es ohne Qt-Designer wartbar bleibt.
"""
from __future__ import annotations

from qgis.PyQt import QtWidgets

from . import points_layer, settings
from .i18n import tr


class OptionsDialog(QtWidgets.QDialog):
    """Modaler Einstellungsdialog. ``exec()`` liefert Accepted, wenn gespeichert
    wurde."""

    def __init__(self, parent, lang: str = "de") -> None:
        super().__init__(parent)
        self.lang = lang
        self.setWindowTitle(tr("opt_title", lang))

        cfg = settings.all_settings()

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(16, 16, 16, 12)
        form.setSpacing(8)

        self.base_url = QtWidgets.QLineEdit(cfg["base_url"])
        self.base_url.setMinimumWidth(420)
        self.api_key = QtWidgets.QLineEdit(cfg["api_key"])
        # Schluessel maskiert anzeigen, aber kopierbar lassen.
        self.api_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.PasswordEchoOnEdit)

        self.temp = self._unit_combo("temperature_unit", cfg["temperature_unit"])
        self.wind = self._unit_combo("wind_unit", cfg["wind_unit"])
        self.precip = self._unit_combo("precipitation_unit", cfg["precipitation_unit"])

        form.addRow(tr("opt_base_url", lang), self.base_url)
        form.addRow(tr("opt_api_access", lang), self.api_key)
        form.addRow(tr("opt_temp", lang), self.temp)
        form.addRow(tr("opt_wind", lang), self.wind)
        # Diagramm-Symbol im Sammel-Layer: legt je Abfrage eine PNG-Datei an,
        # deshalb sichtbar abschaltbar statt stillschweigend an.
        self.chart_marker = QtWidgets.QCheckBox(tr("opt_chart_marker_hint", lang))
        self.chart_marker.setChecked(settings.flag("chart_marker"))

        form.addRow(tr("opt_precip", lang), self.precip)
        form.addRow(tr("opt_chart_marker", lang), self.chart_marker)

        # Aufraeumen als Knopf, nicht beim Beenden automatisch: Dateien zu
        # loeschen ist die Entscheidung des Anwenders, und der Ordner liegt
        # neben seinem Projekt.
        self.btn_aufraeumen = QtWidgets.QPushButton(tr("opt_cleanup", lang))
        self.btn_aufraeumen.clicked.connect(self._aufraeumen)
        form.addRow("", self.btn_aufraeumen)

        hint = QtWidgets.QLabel(tr("opt_hint", lang))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6f6f6f;")

        # Ok = AcceptRole (speichert + schliesst), Cancel verwirft.
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def _unit_combo(self, key: str, current: str) -> QtWidgets.QComboBox:
        """Combo mit den erlaubten Werten eines Einheiten-Settings; angezeigt wird
        das Symbol, gespeichert (userData) der API-Wert."""
        combo = QtWidgets.QComboBox()
        for value in settings.allowed(key):
            combo.addItem(settings.symbol_for(key, value), value)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        return combo

    def _aufraeumen(self) -> None:
        """Verwaiste Diagramme loeschen, nach Rueckfrage mit Zahlenangabe."""
        try:
            kandidaten = points_layer.unbenutzte_diagramme()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, tr("opt_cleanup", self.lang), str(e))
            return
        unbenutzt = len(kandidaten)
        if unbenutzt == 0:
            QtWidgets.QMessageBox.information(
                self, tr("opt_cleanup", self.lang),
                tr("opt_cleanup_none", self.lang))
            return
        if QtWidgets.QMessageBox.question(
                self, tr("opt_cleanup", self.lang),
                tr("opt_cleanup_ask", self.lang, n=unbenutzt)
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            # Die eben gezeigte Liste weiterreichen: ein zweiter Durchlauf
            # koennte eine andere Zahl liefern als die, der zugestimmt wurde.
            geloescht, uebrig = points_layer.aufraeumen(kandidaten)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, tr("opt_cleanup", self.lang), str(e))
            return
        QtWidgets.QMessageBox.information(
            self, tr("opt_cleanup", self.lang),
            tr("opt_cleanup_done", self.lang, n=geloescht, k=uebrig))

    def _save_and_accept(self) -> None:
        settings.set_value("base_url", self.base_url.text().strip()
                           or settings.DEFAULTS["base_url"])
        settings.set_value("api_key", self.api_key.text().strip())
        settings.set_value("temperature_unit", self.temp.currentData())
        settings.set_value("wind_unit", self.wind.currentData())
        settings.set_value("precipitation_unit", self.precip.currentData())
        settings.set_flag("chart_marker", self.chart_marker.isChecked())
        self.accept()
