# Datos y preparación — Planificador geoespacial de eclipses

El proyecto desarrolla la Idea 3 de [01_ideas_producto.md](01_ideas_producto.md): un planificador que permite elegir un eclipse real y comparar lugares de observación con información astronómica y meteorológica. El producto reúne los eclipses solares y lunares de los **próximos tres años** y prepara sus datos antes de la consulta. El usuario abre un selector local, elige una fecha del catálogo NASA y consulta países, zonas, visibilidad, duración y climatología. Los alojamientos y sus precios son una ampliación posterior.

La preparación se ejecuta con [eclipse_pipeline.py](../../src/eclipse_pipeline.py), apoyado en los módulos de [catálogos](../../src/eclipse_catalogs.py), [ubicaciones](../../src/eclipse_locations.py), [acceso a fuentes](../../src/eclipse_sources.py) y [exportación](../../src/eclipse_product.py). El [notebook](02_planificador_eclipse.ipynb) recorre el flujo para un evento de ejemplo (2 de agosto de 2027) y la [interfaz](../../app/index.html) consulta la exportación local.

## 1. Qué datos necesita el producto

**Observación.** La unidad básica del dataset es **evento × ubicación × año histórico × día relativo**, medida a la hora aproximada del máximo local del eclipse. Se agrega en **evento × ubicación** para comparar destinos, y en **evento** para el catálogo del selector. El pronóstico, cuando existe, se guarda como **evento × ubicación × hora de pronóstico**.

| Tipo | Datos | Motivo |
|---|---|---|
| **Imprescindibles** | Catálogo de eclipses (fecha, familia, tipo, identificador NASA, ΔT, coordenadas del máximo) | Solo se planifican eventos reales; una fecha que no corresponde a un eclipse se rechaza. |
| | Elementos besselianos (solares) o punto sublunar (lunares) | Calcular fase, ocultación, altura y duración en cada ubicación. |
| | Coordenadas de ubicaciones candidatas | Sin ellas no hay comparación de destinos. |
| | Nubosidad y lluvia horarias históricas a la hora del máximo | Son la base de la frecuencia de cielo despejado, el criterio que más pesa en la decisión. |
| **Deseables** | Temperatura, viento, humedad, presión, radiación y otras variables horarias | Contexto de comodidad y de calidad de la observación. |
| | Pronóstico operativo | Solo aporta valor en los 16 días anteriores al evento. |
| | Elevación, país, polígonos cartográficos | Precisión geométrica, agrupación por país y mapa. |
| | Accesibilidad, relieve del horizonte, alojamiento y precio | Fase posterior; no se dispone todavía de una fuente adecuada. |

**Granularidad e histórico.** La meteorología es **horaria**: incluye todas las horas del día del evento y, por defecto, **dos días antes y dos después** (margen configurable de 0 a 30 días). La climatología usa **30 años recientes disponibles** con la misma fecha, anteriores al año del eclipse; una clasificación se considera validada con al menos **15 años**. La ventana de planificación es siempre **[hoy, hoy + 3 años)** en UTC: el pipeline parte de la fecha en que se ejecuta y la interfaz la recalcula al abrirse. En la preparación entregada (octubre de 2026) contiene **14 eclipses, 7 solares y 7 lunares**, entre el 6 de febrero de 2027 y el 11 de julio de 2029.

**Candidatos.** Sin CSV propio se evalúan **688 ciudades NASA** y referencias astronómicas sobre la línea central o el máximo. Tras descartar puntos sin el astro sobre el horizonte, se eligen hasta **12 candidatos** para la climatología: como máximo una cuarta parte son referencias astronómicas (que pueden caer en el mar y no llegan al selector), y entre las ciudades se toma primero la mejor de cada país. Así la climatología cubre más países: por ejemplo, España en el eclipse de 2027. Un CSV propio admite `name`, `latitude`, `longitude` y, opcionalmente, `elevation_m` y `country_code` (ISO de dos letras).

## 2. Fuentes reales y viabilidad

| Fuente | Acceso | Cobertura y actualización | Estabilidad | Alternativa |
|---|---|---|---|---|
| [Catálogos solar](https://eclipse.gsfc.nasa.gov/SEcat5/catalog.html) y [lunar](https://eclipse.gsfc.nasa.gov/LEcat5/LEcatalog.html) NASA GSFC | HTML estático, descarga y análisis de las tablas `<pre>` | Siglos hasta el año 3000; predicciones fijas que solo cambian con revisiones de ΔT | Web histórica sin API; puede reorganizarse | [EclipseWise](https://www.eclipsewise.com/) (mismo autor) o cálculo propio con efemérides JPL y Skyfield |
| [Elementos besselianos](https://eclipse.gsfc.nasa.gov/SEsearch/SEdata.php?Ecl=20270802) y trayectorias NASA por evento | HTML enlazado desde cada fila del catálogo | Todos los solares del catálogo; la trayectoria central solo existe en totales, anulares e híbridos | Igual que el catálogo | Elementos publicados en EclipseWise o calculados con efemérides |
| Ciudades del explorador NASA (`JSEX/city-*.js`) | Ficheros JavaScript estáticos | 688 ciudades de seis regiones, con elevación | Estática | [GeoNames](https://www.geonames.org/) `cities15000` (CC BY 4.0) |
| [Natural Earth 1:110m](https://www.naturalearthdata.com/downloads/110m-cultural-vectors/110m-admin-0-countries/) | GeoJSON descargado una vez | Países y territorios; versiones esporádicas | Alta, dominio público | geoBoundaries o GADM |
| [Open-Meteo Historical Weather](https://open-meteo.com/en/docs/historical-weather-api) | API REST JSON, sin clave | ERA5 horario desde 1940, con unos 5 días de retraso | Servicio gratuito no comercial con cuotas: 600 llamadas/minuto, 5.000/hora y 10.000/día | [Copernicus CDS](https://cds.climate.copernicus.eu/) (ERA5 original, con cuenta gratuita) o NASA POWER |
| [Open-Meteo Forecast](https://open-meteo.com/en/docs) | API REST JSON | Hasta 16 días, actualizado varias veces al día | Igual que el histórico | MET Norway Locationforecast |

Se solicitan **19 variables históricas**: `temperature_2m`, `relative_humidity_2m`, `dew_point_2m`, `apparent_temperature`, `pressure_msl`, `surface_pressure`, `cloud_cover` y sus capas baja, media y alta, `precipitation`, `rain`, `snowfall`, `weather_code`, `wind_speed_10m`, `wind_direction_10m`, `wind_gusts_10m`, `shortwave_radiation` y `sunshine_duration`. Las unidades son °C, %, hPa, mm (cm para nieve), km/h, grados, W/m², segundos y código WMO. El pronóstico añade `precipitation_probability` (%), `visibility` (m) y `showers` (mm): **22 variables**. Las consultas históricas fijan `models=era5` y hora GMT, y se conservan las coordenadas efectivas de la rejilla.

**Viabilidad práctica.** Una consulta con 12 ubicaciones, 19 variables y 7 días cuenta como 24 llamadas para la cuota de Open-Meteo. Preparar los 14 eventos con 30 años supone unas 10.000 llamadas ponderadas, el límite diario. El pipeline espaciará las peticiones para respetar la cuota horaria, esperará cuando se supere el límite por minuto u hora, y se detendrá de forma ordenada si se agota la cuota diaria. Cada respuesta anual queda guardada, así que repetir la orden reanuda la adquisición sin volver a descargar lo obtenido.

## 3. Uso, privacidad y datos personales

- **NASA:** se conserva la atribución *Eclipse Predictions by Fred Espenak, NASA/GSFC*. Las claves [solares](https://eclipse.gsfc.nasa.gov/SEcat5/SEcatkey.html) y [lunares](https://eclipse.gsfc.nasa.gov/LEcat5/LEcatkey.html) documentan magnitudes, tiempos y duraciones.
- **Open-Meteo y ERA5:** datos bajo [CC BY 4.0](https://open-meteo.com/en/license), con atribución a [Open-Meteo](https://doi.org/10.5281/zenodo.7970649) y [ERA5/ECMWF](https://doi.org/10.24381/cds.adbb2d47). El uso gratuito es no comercial y respeta los [términos del servicio](https://open-meteo.com/en/terms); un uso comercial requeriría el plan de pago o los datos de Copernicus.
- **Natural Earth:** [dominio público](https://www.naturalearthdata.com/about/terms-of-use/).
- **Datos personales:** no se recogen. El selector funciona con archivos locales, sin cuentas, sin identidad de viajeros y sin enviar la selección a ningún proveedor. Solo se usan destinos públicos y datos ambientales. Un CSV aportado debe evitar coordenadas de domicilios u otros lugares personales.

## 4. Del dato bruto al dato utilizable

```text
NASA (catálogos, elementos, ciudades) ─┐
Open-Meteo ERA5 / pronóstico ──────────┼─► Bronze (originales + metadatos) ─► Silver (limpieza y geometría) ─► Gold (muestras, ranking) ─► selector local
Natural Earth ─────────────────────────┘
```

Reproducción desde la raíz del proyecto, con Python 3.10 o posterior:

```powershell
python -m pip install -r requirements.txt
python src/eclipse_pipeline.py                 # ventana [hoy, hoy + 3 años): adquisición y preparación (necesita red)
python src/eclipse_pipeline.py --export-only   # exportación del selector para la ventana de hoy, sin red
python -W error::DeprecationWarning -m unittest discover -s tests
```

Después se abre `app/index.html` en el navegador, sin servidor. La interfaz toma la fecha del día: oculta los eclipses ya pasados y muestra como pendientes los que entran nuevos en la ventana. Otras opciones: `--catalog-only` (solo catálogo), `--offline` (reconstruir solo a partir de Bronze verificado), `--refresh` (volver a descargar), `--event-date` y `--family` (un evento concreto) y `--locations` (CSV propio).

### Bronze: originales y trazabilidad

- `data/bronze/nasa/`: índices y catálogos por siglo, y las listas de ciudades.
- `data/bronze/events/<fecha>/`: elementos besselianos, trayectoria, una respuesta climática por año y el manifiesto con fallos y ausencias.
- `data/bronze/geography/`: el GeoJSON original de Natural Earth.

Cada respuesta se guarda tal como llega, con un `.metadata.json` que registra URL, parámetros, fecha UTC de adquisición, estado HTTP y SHA-256. La configuración forma parte del nombre de las respuestas climáticas (`climate_<año>_<hash>.json`), para no reutilizar por error otra ventana o conjunto de coordenadas. `--offline` solo acepta copias cuyo hash coincide.

El repositorio incluye los originales NASA, la cartografía y **las respuestas climáticas de 2022 a 2026 del eclipse de ejemplo** (2027-08-02, unos 1,1 MB), que permiten reconstruirlo sin red (ver *Trazabilidad*). El resto de respuestas climáticas (unos 85 MB) se regenera con el pipeline y está excluido en `.gitignore`.

### Silver: transformaciones

| Archivo | Transformación |
|---|---|
| `planning_eclipse_catalog.csv` | Tablas NASA convertidas a filas tipadas. Se normalizan fechas ISO y coordenadas con signo, duraciones en segundos y hora TD convertida a UT aproximado con ΔT. Se rechazan tipos desconocidos, identificadores duplicados y coordenadas fuera de rango, y se filtra la ventana de planificación. |
| `nasa_cities.csv` | Grados, minutos y segundos pasados a decimales, duplicados eliminados y país asignado con su procedencia: contexto regional NASA (por ejemplo, `CA` en Norteamérica puede ser California), nombres normalizados o polígono aproximado. |
| `events/<fecha>/all_visible_candidates.csv` | Circunstancias locales de cada candidato: fase, magnitud, fracción de disco solar ocultada, altura, hora del máximo y duraciones central y visible. Se descartan los candidatos sin el astro sobre el horizonte; una elevación ausente se asume cero y se marca. |
| `events/<fecha>/locations.csv` | Candidatos seleccionados para la climatología (apartado 1). |
| `events/<fecha>/climate_hourly.csv` | Las 19 variables horarias en UTC, con año de referencia y `day_offset`. Se validan continuidad horaria, unidades, porcentajes en [0, 100], valores no negativos, coherencia lluvia ≤ precipitación y ausencia de infinitos. Los valores ausentes se mantienen nulos, nunca cero. Se contemplan los cambios de día y de año en UTC. |

La nubosidad del máximo usa la hora más cercana. Lluvia y precipitación usan el intervalo que contiene el máximo, porque la API informa el acumulado de la hora anterior.

### Gold: el dataset para la fase siguiente

**Qué se sabe al planificar.** La consulta se hace meses o años antes del eclipse. En ese momento se conocen la ubicación, la geometría del evento y la meteorología de **años anteriores**, pero no la nubosidad ni la lluvia de la hora del eclipse, que son precisamente lo que se quiere estimar. Las etiquetas `clear_sky_proxy` (nubosidad ≤20 %) y `rain_proxy` (lluvia ≥0,1 mm/h) se calculan con esos valores. Si las 19 variables de esa misma hora se usaran como predictores, un modelo solo aprendería a reproducir los umbrales. Por eso Gold separa dos tablas:

**`data/gold/events/<fecha>/climate_event_samples.csv`**: observaciones históricas, base de las etiquetas y de la climatología.

- **Fila:** un evento × una ubicación × un año histórico × un día relativo (−2 a +2), a la hora del máximo local. Con 12 ubicaciones y 30 años son 1.800 filas por evento; los 14 eclipses de la preparación entregada suman 23.850.
- **Columnas:** identificadores (`sample_id`, `event_id`, `name`, `reference_year`, `day_offset`), las 19 variables a esa hora, la celda ERA5 (`grid_latitude`, `grid_longitude`) y las dos etiquetas.
- **Uso:** describir el tiempo de cada año y calcular las frecuencias del ranking. Sus variables meteorológicas **no son predictores**: se observan a la vez que la etiqueta.

**`data/gold/events/<fecha>/planning_ml_dataset.csv`**: el dataset de entrada para Machine Learning. Hay uno por eclipse, y juntos forman el conjunto de entrenamiento.

- **Fila:** un evento × una ubicación × un año (día 0). Las filas `history` son los 30 años con etiqueta conocida. La fila `query` es la del propio eclipse: lleva los mismos predictores, calculados con todo el histórico, y la etiqueta vacía; es exactamente lo que recibe el modelo en una consulta. Son 31 filas por ubicación, 4.929 en los 14 eclipses (4.770 con etiqueta).
- **Predictores, todos conocidos al planificar:** ubicación (`latitude`, `longitude`, `elevation_m`, celda ERA5), fecha y hora (`day_of_year`, `local_solar_hour`), altura del astro (`body_altitude_deg`) y climatología de **años estrictamente anteriores** a la fila (`prior_years`, `prior_clear_frequency`, `prior_rain_frequency`, `prior_window_clear_frequency` con los días −2 a +2, `prior_mean_cloud_cover_pct` y `prior_mean_cloud_cover_low_pct`). La fila de 2010 solo usa 1997–2009, igual que una consulta hecha en 2010.
- **Línea base:** `baseline_clear_probability` y `baseline_rain_probability`, la frecuencia histórica previa. Es la referencia que un modelo debe superar.
- **Etiquetas:** `clear_sky_proxy` y `rain_proxy` del año de la fila.
- **Uso y validación:** clasificación de cielo despejado y lluvia con validación temporal por `reference_year` (entrenar con años anteriores y probar con posteriores), evaluando solo filas con al menos 10 años previos (3.180). Como referencia, la línea base obtiene un Brier de 0,138 en esas filas, con un 25,7 % de casos despejados. El informe de calidad registra el papel de cada columna (`ml_dataset.columns`).
- **Fuera del modelo histórico:** el pronóstico operativo solo existe en los 16 días anteriores, y los pronósticos archivados de Open-Meteo (Historical Forecast API) cubren solo los últimos años, muy pocos para entrenar frente a 30 años de ERA5. Se mantiene como capa separada del ranking (ver abajo), no como predictor de entrenamiento.

**`data/gold/events/<fecha>/location_ranking.csv`** resume la información por destino.

- **Fila:** un evento × una ubicación candidata.
- **Columnas principales:** geometría (`local_phase`, `obscuration_fraction`, `body_altitude_deg`, `event_time_utc_approx`, `central_duration_s`, `visible_eclipse_duration_s`); climatología (`clear_probability`, `rain_probability`, años válidos e intervalos de Wilson al 95 %); y la valoración (`score`, `rank`, `ranking_validated_coverage`, `ranking_basis`).
- **Uso:** ordenar destinos en el selector y servir de referencia frente a la que comparar el modelo.

La clasificación prioriza la fase astronómica y una cobertura de al menos 15 años antes de aplicar la puntuación:

- **Solares:** 40 % cielo despejado, 15 % ausencia de lluvia, 20 % ocultación, 15 % duración y 10 % altura del Sol.
- **Lunares:** 45 % cielo despejado, 20 % ausencia de lluvia y 35 % altura de la Luna. La duración publicada es global, igual en todos los destinos, y no discrimina entre ellos.

Si todos los candidatos tienen un pronóstico vigente a la hora del evento, se usa con los mismos pesos y `ranking_basis` lo indica. Los pesos son una decisión inicial explicable, pendiente de validación.

Además, por evento se guardan `destination_ranking.csv` (solo destinos terrestres), `astronomical_references.csv`, `climate_daily_summary.csv` (estadísticas a la hora del evento por día relativo), `climate_daily_conditions.csv` (mínimas, máximas y acumulados de 24 horas), `operational_forecast.csv`, `forecast_status.json` y `pipeline_quality_report.json`. Este último registra configuración, fallos, ausencias, pesos, papel de las columnas de ML, el SHA-256 de cada salida y el del código que la generó (`code_sha256`).

**Exportación al selector.** `data/gold/planner_data.js` (y su equivalente `.json`) combina el catálogo astronómico de destinos terrestres de Silver con los resultados Gold, enlazados por **nombre y coordenadas**. Además de la ventana, incluye como pendientes los eclipses de los dos años siguientes. Así la interfaz mantiene «hoy + 3 años» aunque se abra meses después sin ejecutar el pipeline; si la ventana supera esos datos, avisa. Las zonas sin climatología aparecen como «climatología pendiente» y nunca reciben datos de otra ubicación. Para eclipses solares, el selector de país usa la **máxima ocultación entre los puntos evaluados** de cada país y muestra los que alcanzan el **70 %**. El umbral se exporta con los datos y la interfaz lo lee de ahí. En lunares se ordena por la altura de la Luna.

**Estados del selector.** Un evento solo se presenta como preparado si su informe registra la configuración usada, tiene al menos 15 años de histórico y no tiene fallos ni ausencias.

- Las preparaciones hechas con otra configuración (ubicaciones, años o ventana meteorológica) quedan pendientes.
- Las de histórico corto se muestran como parciales.
- Si una actualización falla, los resultados anteriores se muestran como parciales con un aviso.
- `data/gold/batch_acquisition_report.json` registra el resultado de cada evento en la última ejecución.

### Trazabilidad y ejemplo reconstruible

Los datos y los informes entregados proceden de **una misma ejecución y de la misma versión del código**. Todas las salidas se escriben con fin de línea LF, y `.gitattributes` impide que git convierta `data/`. Así, el SHA-256 de cada informe coincide con el archivo de cualquier copia del repositorio o ZIP, también en Windows. El hash del código se calcula con fin de línea normalizado.

```powershell
python src/eclipse_pipeline.py --verify   # hashes de las salidas Gold, versión del código y respuestas Bronze incluidas
python src/eclipse_example.py             # reconstruye el ejemplo sin red y compara hashes
```

**Ejemplo pequeño:** el eclipse total del 2 de agosto de 2027 con **5 años de histórico (2022–2026)**. `eclipse_example.py` copia en una carpeta temporal solo las fuentes versionadas (catálogos y ciudades NASA, Natural Earth, elementos besselianos, trayectoria y las cinco respuestas climáticas), rehace Silver y Gold con el código actual y compara cada archivo con la referencia de `data/example/2027-08-02/`. No usa red ni escribe en `data/`. Solo se excluye de la comparación `forecast_status.json`, porque registra el día de la consulta. Con 5 años la clasificación no alcanza los 15 años validados: el ejemplo sirve para comprobar la preparación, no para elegir destino.

**Verificación.** La batería de tests cubre catálogos, caché verificada, geometría solar y lunar (incluidos híbridos), ventanas meteorológicas con cambio de día y de año, cuotas de la API, reutilización de preparaciones, estados de la exportación, coherencia del umbral entre datos e interfaz, predictores calculados solo con años anteriores, fin de línea de las salidas, detección de archivos alterados y reconstrucción del ejemplo. Los tests no escriben en `data/`.

## 5. Riesgos y limitaciones

- **Dato que falta:** accesibilidad, relieve del horizonte, alojamiento y precio. La recomendación todavía no tiene en cuenta si se puede llegar al lugar ni a qué coste.
- **Sesgo y limitaciones:** solo se comparan las ciudades disponibles, sin buscar el óptimo en todo el territorio. ERA5 es una rejilla de unos 30 km, no una medición local. Las circunstancias solares son aproximadas y, en los lunares, la visibilidad se evalúa solo en el máximo global.
- **Si una fuente deja de estar disponible:** Bronze conserva los originales y permite reconstruir sin red (`--offline`). Open-Meteo se sustituiría por ERA5 de Copernicus CDS y los catálogos NASA por EclipseWise o por efemérides JPL.
