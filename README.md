# Planificador geoespacial de eclipses

El proyecto prepara los eclipses solares y lunares de los **próximos tres años** a partir del catálogo NASA. Una aplicación local permite elegir un evento por fecha y consultar países, zonas, circunstancias astronómicas y climatología. Los alojamientos y sus precios se incorporarán más adelante.

- [Datos y preparación](docs/entregas/02_datos_y_preparacion.md): fuentes, variables, flujo Bronze → Silver → Gold y limitaciones.
- [Notebook](docs/entregas/02_planificador_eclipse.ipynb): ejemplo reproducible de selección y análisis.
- [Pipeline](src/eclipse_pipeline.py): adquisición y preparación de los datos.
- [Selector](app/index.html): interfaz local que explora los resultados preparados.

## Uso

Desde la raíz del proyecto, con Python 3.10 o posterior:

```powershell
python -m pip install -r requirements.txt
python src/eclipse_pipeline.py
```

La primera ejecución necesita Internet y prepara la ventana completa: catálogo, circunstancias locales, ubicaciones, climatología y archivos del selector. Después basta con abrir `app/index.html` en el navegador (o `Ctrl + O` y elegir ese archivo). No necesita servidor, y elegir un eclipse usa los datos guardados sin hacer nuevas descargas.

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
# Ejecutar los tests.
python -W error::DeprecationWarning -m unittest discover -s tests
```

La ventana empieza hoy (UTC), incluido, y termina tres años después, sin incluir ese día. La interfaz la recalcula al abrirse: los eclipses pasados desaparecen y los que entran nuevos aparecen como pendientes hasta que se vuelva a ejecutar el pipeline. En octubre de 2026 reúne 14 eventos, 7 solares y 7 lunares, entre el 6 de febrero de 2027 y el 11 de julio de 2029. La climatología usa **30 años históricos** disponibles a la fecha de ejecución y 19 variables horarias del día del evento y de los dos días anteriores y posteriores.

## Reanudar la adquisición

Open-Meteo limita el uso gratuito a 600 llamadas por minuto, 5.000 por hora y 10.000 al día, y preparar la ventana completa ronda ese límite diario. El pipeline espacia las peticiones, espera cuando se supera el límite por minuto u hora y se detiene si se agota la cuota diaria. Basta con volver a ejecutar `python src/eclipse_pipeline.py` más tarde: las respuestas ya guardadas se reutilizan y los eventos completos no se vuelven a preparar.

## Cómo leer el selector

Un evento solo aparece como **preparado** si tiene al menos 15 años de histórico y no le faltan años ni valores. Los demás casos se marcan así:

- **Parcial:** faltan datos o la última actualización falló; se muestran los resultados anteriores con un aviso.
- **Pendiente:** la climatología no está preparada o se preparó con otra configuración.

`data/gold/batch_acquisition_report.json` resume el resultado de la última ejecución.

**Países y zonas.** En eclipses solares, los países se ordenan por la mayor ocultación del disco solar entre sus zonas analizadas, y se muestran los que llegan al **70 %**. Es el máximo de los puntos evaluados, no el porcentaje del territorio. En lunares se ordena por la altura de la Luna en el máximo global, y la duración mostrada es la global de la fase. Las zonas sin climatología preparada aparecen como «climatología pendiente» y nunca reciben datos de otra ubicación.

**Candidatos.** Sin CSV propio se evalúan 688 ciudades NASA y referencias astronómicas, y se eligen hasta 12 candidatos para la climatología: como máximo una cuarta parte son referencias, que pueden caer en el mar, y entre las ciudades se toma primero la mejor de cada país. Un CSV propio (`--event-date ... --locations archivo.csv`) admite `name`, `latitude`, `longitude` y, opcionalmente, `elevation_m` y `country_code` (ISO de dos letras, por ejemplo `ES` para Melilla o `GI` para Gibraltar).

**Mapa.** Usa una copia local de [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/) a escala 1:110 millones, de dominio público. Sirve para situar destinos, no para representar la franja de visibilidad.

## Datos en el repositorio

Se versionan:

- los originales NASA y la cartografía;
- las respuestas climáticas de 2022 a 2026 del eclipse de ejemplo (`data/bronze/events/2027-08-02/`, unos 1,1 MB) y su resultado de referencia (`data/example/2027-08-02/`), para reconstruirlo sin red;
- las capas Silver y Gold ligeras de todos los eventos, incluido el dataset de ML `planning_ml_dataset.csv`, generadas en una misma ejecución;
- la exportación que abre el selector (`data/gold/planner_data.js`).

Las respuestas climáticas completas, los ficheros horarios de Silver y los catálogos auxiliares hasta el año 3000 se regeneran con el pipeline y están en `.gitignore`.

Para entregar el proyecto, genera el ZIP desde git tras confirmar los cambios (`git archive -o entrega.zip HEAD`): así contiene exactamente los archivos versionados, sin restos locales, y `python src/eclipse_pipeline.py --verify` coincide en la copia descomprimida.
