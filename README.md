# Planificador geoespacial de eclipses

El proyecto prepara los eclipses solares y lunares de los **próximos tres años** a partir del catálogo NASA. Una aplicación local permite elegir un evento por fecha y consultar países, zonas, circunstancias astronómicas y climatología. Al elegir un destino, consulta en directo su pronóstico y los alojamientos cercanos, con enlaces a Google Maps y Booking para las fechas del eclipse.

- [Datos y preparación](docs/entregas/02_datos_y_preparacion.md): fuentes, variables, flujo Bronze → Silver → Gold y limitaciones.
- [Notebook](docs/entregas/02_planificador_eclipse.ipynb): ejemplo reproducible de selección y análisis.
- [Machine Learning](docs/entregas/03_machine_learning.md): problema, baseline, validación por años y decisión para el producto.
- [Diseño del frontal y UX](docs/entregas/04_diseno_frontal_ux.md): flujo de usuario, mockup de la pantalla principal y continuidad hacia el MVP.
- [Pipeline](src/eclipse_pipeline.py): adquisición y preparación de los datos.
- [Experimento de ML](src/ml_planning_experiment.py): reproduce sin red las métricas de `data/gold/ml/` a partir del Gold.
- [Selector](app/index.html): interfaz local que explora los resultados preparados.
- [Guía de tipos de eclipse](app/eclipses.html): explicación de cada tipo con esquemas propios y fotos de Wikimedia Commons guardadas en `app/img/eclipses/` (autor y licencia en el pie de cada una).

## Uso

Desde la raíz del proyecto, con Python 3.10 o posterior:

```powershell
python -m pip install -r requirements.txt
python src/eclipse_pipeline.py
```

La primera ejecución necesita Internet y prepara la ventana completa: catálogo, circunstancias locales, ubicaciones, climatología y archivos del selector. Después basta con abrir `app/index.html` en el navegador (o `Ctrl + O` y elegir ese archivo). No necesita servidor, y elegir un eclipse usa los datos guardados sin hacer nuevas descargas. Solo el panel del destino necesita conexión (ver «Panel del destino»).

```powershell
# Solo el catálogo de la ventana, sin climatología.
python src/eclipse_pipeline.py --catalog-only
# Rehacer la exportación del selector con los resultados existentes, sin red.
python src/eclipse_pipeline.py --export-only
# Volver a descargar las fuentes y recalcular.
python src/eclipse_pipeline.py --refresh
# Comprobar que datos, informes y código son de la misma versión.
python src/eclipse_pipeline.py --verify
# Reconstruir sin red el ejemplo pequeño (2027-08-02, 2022–2026) y comparar hashes.
python src/eclipse_example.py
# Reproducir el experimento de ML y comparar con los resultados guardados, sin red.
python src/ml_planning_experiment.py --check
# Ejecutar los tests.
python -W error::DeprecationWarning -m unittest discover -s tests
```

La ventana empieza hoy (UTC), incluido, y termina tres años después, sin incluir ese día. La interfaz la recalcula al abrirse: los eclipses pasados desaparecen y los que entran nuevos aparecen como pendientes hasta que se vuelva a ejecutar el pipeline. En octubre de 2026 reúne 14 eventos, 7 solares y 7 lunares, entre el 6 de febrero de 2027 y el 11 de julio de 2029. La climatología usa **30 años históricos** disponibles a la fecha de ejecución y 19 variables horarias del día del evento y de los dos días anteriores y posteriores.

## Climatología del resto de zonas con Copernicus

Open-Meteo limita las llamadas por ubicación, así que solo se usa para los destinos del ranking. Para las demás zonas del selector (unas 600 en los eclipses solares), `src/eclipse_era5_cds.py` descarga el mismo reanálisis ERA5 del [Copernicus Climate Data Store](https://cds.climate.copernicus.eu/), una serie por celda de 0,25°, sin cuota por ubicación:

```powershell
python src/eclipse_era5_cds.py                    # todos los eclipses solares preparados (varias horas: el CDS atiende de una en una)
python src/eclipse_era5_cds.py --event-date 2027-08-02
python src/eclipse_pipeline.py --export-only      # llevar el resultado al selector
```

Requiere una cuenta gratuita del CDS, aceptar la licencia de «ERA5 hourly time-series data on single levels» y el token en `%USERPROFILE%\.cdsapirc` (nunca dentro del repositorio):

```
url: https://cds.climate.copernicus.eu/api
key: <token personal>
```

Cada zona usa la celda ERA5 en tierra más cercana, como Open-Meteo. Las zonas que tienen las dos fuentes se comparan en `data/gold/events/<fecha>/era5_zone_report.json`. El selector indica en la cabecera de ubicaciones cuántas zonas usan esta fuente; estas zonas no entran en el ranking climático del Gold (`destination_ranking.csv`).

## Reanudar la adquisición

Open-Meteo limita el uso gratuito a 600 llamadas por minuto, 5.000 por hora y 10.000 al día, y preparar la ventana completa ronda ese límite diario. El pipeline espacia las peticiones, espera cuando se supera el límite por minuto u hora y se detiene si se agota la cuota diaria. Basta con volver a ejecutar `python src/eclipse_pipeline.py` más tarde: las respuestas ya guardadas se reutilizan y los eventos completos no se vuelven a preparar.

## Panel del destino

«Elegir como destino» en una tarjeta, o un punto del mapa, abre un panel que consulta en ese momento ([`app/live.js`](app/live.js)):

- **Pronóstico** de [Open-Meteo](https://open-meteo.com/) a la hora del máximo, cuando faltan 16 días o menos. Antes indica desde qué fecha estará disponible.
- **Alojamientos** de [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL) a menos de 8 km, o de 25 km si apenas hay, mediante la API Overpass. Se consultan tres instancias públicas a la vez y, si fallan, [Photon](https://photon.komoot.io/) como búsqueda parcial. El mapa usa teselas de Esri y [Leaflet](https://leafletjs.com/) desde cdnjs, con verificación de integridad.
- **Fechas y personas.** Por defecto, la noche anterior y la del eclipse para 2 adultos en una habitación; se pueden cambiar entrada, salida (hasta 30 noches), adultos (hasta 8) y habitaciones. Al pulsar «Consultar» se actualizan los precios y los enlaces de reserva.
- **Disponibilidad y precio para esas fechas** de los alojamientos cercanos, si la app se abre con el servidor local (ver abajo): tarifa más baja por habitación y noche, total de la estancia y agencia. Siempre hay además enlaces a Google Maps y Booking con las fechas y personas ya rellenadas.
- **Fotos del alojamiento.** Al pulsar el nombre de un alojamiento con precio se muestran su foto de TripAdvisor y las fotos de [Wikimedia Commons](https://commons.wikimedia.org/) tomadas a menos de 150 m (cada una con su autor y licencia), con enlaces a Google Imágenes y a TripAdvisor para ver más.

Solo se envían las coordenadas del destino (y, al servidor de precios, las fechas y el número de personas). Sin conexión, el panel lo indica y el resto de la app funciona igual.

## Precios de alojamiento (servidor local)

El navegador no puede pedir los precios directamente, así que se sirven desde un pequeño servidor local:

```powershell
python src/app_server.py        # abre http://127.0.0.1:8000/app/index.html
```

Para cada destino, [`src/app_server.py`](src/app_server.py) busca su localidad en TripAdvisor (una vez; queda en `.cache/`), obtiene los alojamientos cercanos y consulta la tarifa más baja por habitación y noche, en euros, que publican Booking, Agoda y otras agencias para las fechas y personas elegidas. Que alguna agencia publique tarifa se muestra como «Disponible»; si ninguna lo hace, «Sin oferta para esas fechas». Es una disponibilidad orientativa: no distingue un hotel completo de uno que aún no ha abierto esas fechas (lo habitual a más de un año vista), y la confirmación está en la agencia. Cambiar las fechas no gasta cuota de RapidAPI, porque la localidad ya está en caché. Usa [Xotelo](https://xotelo.com/), una API gratuita no oficial: puede cambiar o fallar, y entonces quedan los enlaces de búsqueda. Las tarifas desproporcionadas (más de 4 veces la más barata de la zona y más de 500 €) se marcan como atípicas.

La búsqueda de localidad necesita una clave gratuita de RapidAPI:

1. Crea una cuenta en [RapidAPI](https://rapidapi.com/) y suscríbete al plan gratuito de [Xotelo Hotel Prices](https://rapidapi.com/anastue-pGK7lGUO-Wo/api/xotelo-hotel-prices).
2. Guarda la clave en la variable de entorno `RAPIDAPI_KEY` o en el archivo `%USERPROFILE%\.rapidapi_key` (una sola línea), **nunca dentro del repositorio**.

Sin clave, el servidor funciona igual y el panel muestra solo los enlaces. El servidor escucha solo en `127.0.0.1` y sirve únicamente la app y su exportación. También entrega las fotos de TripAdvisor (el navegador no siempre puede cargarlas directamente), y solo desde su CDN: no reenvía otras direcciones.

## Cómo leer el selector

Un evento solo aparece como **preparado** si tiene al menos 15 años de histórico y no le faltan años ni valores. Los demás casos se marcan así:

- **Parcial:** faltan datos o la última actualización falló; se muestran los resultados anteriores con un aviso.
- **Pendiente:** la climatología no está preparada o se preparó con otra configuración.

`data/gold/batch_acquisition_report.json` resume el resultado de la última ejecución.

**Países y zonas.** En eclipses solares, los países se ordenan por la mayor ocultación del disco solar entre sus zonas analizadas, y se muestran los que llegan al **70 %**. Es el máximo de los puntos evaluados, no el porcentaje del territorio. En lunares se ordena por la altura de la Luna en el máximo global, y la duración mostrada es la global de la fase. Las zonas sin climatología preparada aparecen como «climatología pendiente» y nunca reciben datos de otra ubicación.

**Candidatos.** Sin CSV propio se evalúan 688 ciudades NASA y referencias astronómicas. En los eclipses solares se añaden además localidades de [GeoNames](https://www.geonames.org/) (CC BY 4.0) dentro de la zona de mayor ocultación, para que aparezcan lugares que la lista NASA no incluye (por ejemplo, Ceuta, Tarifa, Cádiz o Málaga en la totalidad de 2027). Para la climatología se eligen hasta 12 candidatos: como máximo una cuarta parte son referencias, que pueden caer en el mar, y entre las ciudades se toma primero la mejor de cada país. Si GeoNames encuentra en un país una fase central (total o anular) que no tiene ninguna de esas ciudades, se añaden la localidad de mayor duración y la más poblada de esa fase, en una petición aparte para reutilizar lo ya descargado (en 2027, Ceuta y Málaga). Las demás zonas se muestran con la climatología pendiente. Un CSV propio (`--event-date ... --locations archivo.csv`) admite `name`, `latitude`, `longitude` y, opcionalmente, `elevation_m` y `country_code` (ISO de dos letras, por ejemplo `ES` para Melilla o `GI` para Gibraltar).

**Mapa.** Usa una copia local de [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/) a escala 1:110 millones, de dominio público. Sirve para situar destinos, no para representar la franja de visibilidad.

## Datos en el repositorio

Se versionan:

- los originales NASA, el nomenclátor GeoNames (`data/bronze/geonames/`, 3,4 MB) y la cartografía;
- las respuestas climáticas de 2022 a 2026 del eclipse de ejemplo (`data/bronze/events/2027-08-02/`, unos 1,1 MB) y su resultado de referencia (`data/example/2027-08-02/`), para reconstruirlo sin red;
- las capas Silver y Gold ligeras de todos los eventos, incluido el dataset de ML `planning_ml_dataset.csv`, generadas en una misma ejecución;
- los resultados del experimento de ML (`data/gold/ml/`, unos 400 kB);
- la máscara tierra-mar de ERA5 (`data/bronze/geography/era5_land_sea_mask.nc`, 780 kB) y el resumen de Copernicus por zona (`era5_zone_climate.csv` y su informe);
- la exportación que abre el selector (`data/gold/planner_data.js`).

Las respuestas climáticas completas, los ficheros horarios de Silver y los catálogos auxiliares hasta el año 3000 se regeneran con el pipeline y están en `.gitignore`.

Para distribuir el proyecto, genera el ZIP desde git tras confirmar los cambios (`git archive -o entrega.zip HEAD`): así contiene exactamente los archivos versionados, sin restos locales, y `python src/eclipse_pipeline.py --verify` coincide en la copia descomprimida.
