def classFactory(iface):
    from .weather_picker import WeatherPickerPlugin
    return WeatherPickerPlugin(iface)
