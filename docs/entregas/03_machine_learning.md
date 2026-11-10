# Machine Learning — Planificador geoespacial de eclipses

Esta entrega parte del dataset Gold de la [entrega 2](02_datos_y_preparacion.md) (`data/gold/events/<fecha>/planning_ml_dataset.csv`) y comprueba si un modelo de Machine Learning estima mejor que la climatología histórica el tiempo que hará durante un eclipse.

**Conclusión.** Con los datos disponibles, ningún modelo supera a la climatología con suficiente evidencia, así que el MVP mantiene la **frecuencia histórica** como estimación. Esa referencia sí aporta valor: elegir el destino mejor situado según ella acierta con cielo despejado aproximadamente el doble de veces que elegir al azar.

El experimento se reproduce sin red con [ml_planning_experiment.py](../../src/ml_planning_experiment.py) y sus resultados están en [`data/gold/ml/`](../../data/gold/ml/).

## 1. Qué problema resuelve el modelo

**Funcionalidad.** El selector ordena los destinos de cada eclipse y muestra la probabilidad de cielo despejado y de lluvia. La meteorología pesa un 55 % en la puntuación de los eclipses solares y un 65 % en la de los lunares, y es lo único que la geometría del eclipse no determina.

**Usuario y decisión.** Aficionados, fotógrafos y grupos de astronomía que deciden **a qué destino viajar**, normalmente meses o años antes, cuando todavía no existe ningún pronóstico meteorológico.

**Tipo de problema.** Clasificación binaria probabilística con dos objetivos, cielo despejado y lluvia a la hora del máximo. La probabilidad se usa después para ordenar destinos, por lo que importan tanto su calibración como el orden que produce.

**Pregunta del experimento.** El producto ya dispone de una estimación simple: con qué frecuencia estuvo despejado ese lugar en esa fecha en años anteriores. El modelo solo se justifica si mejora esa cifra.

## 2. Datos de entrada y salida

**Unidad de análisis.** Una fila es un evento × una ubicación × un año histórico. Se usan las filas con al menos 10 años previos: **3.180 filas** de 14 eclipses y 117 ubicaciones. Las 159 filas `query` son las que recibiría el modelo en una consulta real y no tienen etiqueta.

| Papel | Variables del Gold | Uso |
|---|---|---|
| **Objetivos** | `clear_sky_proxy` (nubosidad ≤20 %), `rain_proxy` (lluvia ≥0,1 mm/h) | Lo que se quiere estimar, según ERA5, a la hora del máximo |
| **Referencia** | `baseline_clear_probability`, `baseline_rain_probability` | Climatología de años estrictamente anteriores |
| **Climatología previa** | `prior_clear_frequency`, `prior_rain_frequency`, `prior_window_clear_frequency`, `prior_mean_cloud_cover_pct`, `prior_mean_cloud_cover_low_pct` | Predictores |
| **Lugar y momento** | `latitude`, `longitude`, `elevation_m`, `day_of_year`, `local_solar_hour`, `body_altitude_deg` | Predictores; longitud, día y hora se codifican como ciclos |
| **Descartadas** | `prior_years`, `grid_latitude`, `grid_longitude` | `prior_years` equivale al año y en la consulta vale 30, fuera del rango de entrenamiento; la celda duplica las coordenadas |

**Disponibilidad al usarlo.** Todos los predictores se conocen al planificar: la ubicación y la geometría son fijas, y la climatología procede de años ya transcurridos. La meteorología del día del eclipse no se conoce y no se usa.

| Riesgo | Control |
|---|---|
| **Fuga por variables de la misma hora** (nubosidad o lluvia con las que se calculan las etiquetas) | Excluidas del dataset. Un test impide incorporarlas como predictores. |
| **Fuga temporal en la climatología** | Cada fila usa solo años anteriores a ella. |
| **Fuga entre filas parecidas:** 23 ubicaciones aparecen en varios eventos y hay eclipses separados por dos semanas | Se separa por **años completos**, nunca por filas al azar. |
| **Dependencia entre filas:** el mismo año afecta a muchos destinos | Los intervalos de confianza remuestrean años, no filas. |
| **Sesgo de selección:** los destinos se eligen por visibilidad del eclipse y predominan los tropicales (64 % de las filas de la reserva) | Los resultados valen para esta población, no para cualquier lugar del mundo. |
| **Calidad de la etiqueta:** ERA5 es un reanálisis en celdas de unos 25 km, no una observación del cielo | Se presenta como aproximación («proxy») y se comunica en el producto. |
| **Desigualdad entre eventos:** 2029-07-11 solo tiene 3 ubicaciones | Se excluye de la métrica de ranking, que necesita varios destinos que comparar. |

## 3. Baseline y selección de modelos

Se comparan cinco enfoques, de menor a mayor complejidad y con hiperparámetros fijados de antemano, sin ajuste:

| Enfoque | Papel | Por qué se incluye |
|---|---|---|
| **Prevalencia global** | Referencia mínima | Muestra cuánto aporta conocer el lugar. |
| **Climatología** (frecuencia previa del lugar) | **Baseline** | Es la estimación actual del producto y la que hay que superar. |
| **Climatología calibrada** (regresión logística sobre el logit suavizado de la frecuencia) | Alternativa de ML | Corrige sesgos sistemáticos de la frecuencia con un solo predictor. |
| **Regresión logística completa** | Alternativa de ML | Añade lugar, fecha y nubosidad previa, y sigue siendo explicable. |
| **Gradient boosting** (árboles poco profundos, regularizado) | Alternativa de ML | Permite interacciones no lineales, como latitud × estación. |

**Regla de selección, fijada antes de ver resultados.** Un modelo sustituye a la climatología solo si, en los años de selección, reduce su Brier al menos un **1 %** y el intervalo al 95 % de la diferencia excluye el empate. Si varios cumplen, se elige el más simple a menos de 0,5 puntos del mejor. La complejidad tiene que ganarse: un modelo adicional supone más mantenimiento y es más difícil de explicar al usuario.

## 4. Validación y evaluación

**Estrategia.** Validación temporal progresiva: cada año se predice entrenando solo con años anteriores, tal como se usaría el modelo.

- **Selección (2017–2022, 954 filas):** se aplica la regla anterior.
- **Reserva (2023–2026, 624 filas):** confirma la decisión y no interviene en ella.

No se valida con ubicaciones nuevas porque el producto siempre descarga antes el histórico del lugar consultado.

**Métricas.**

- **Brier (principal):** error de la probabilidad; cuanto menor, mejor.
- **AUC:** capacidad de separar años despejados de nublados.
- **Error de calibración:** distancia media entre lo previsto y lo observado.
- **Valor de la decisión:** con qué frecuencia estuvo despejado el destino que el método habría puesto primero.

**Mejora** es la reducción relativa del Brier frente a la climatología. El **IC 95 %** corresponde a la diferencia de Brier (un valor negativo favorece al modelo), y **años mejores** indica en cuántos años el modelo superó a la climatología.

### Resultados fuera de entrenamiento: cielo despejado

| Modelo | Selección: Brier | AUC | Mejora | IC 95 % | Años mejores | Reserva: Brier | AUC | Mejora | Años mejores |
|---|---|---|---|---|---|---|---|---|---|
| Prevalencia global | 0,1799 | 0,480 | −38,7 % | [+0,037; +0,064] | 0/6 | 0,2123 | 0,519 | −38,9 % | 0/4 |
| **Climatología (baseline)** | **0,1298** | **0,830** | — | — | — | **0,1528** | **0,826** | — | — |
| Climatología calibrada | 0,1280 | 0,829 | +1,4 % | [−0,005; +0,002] | 5/6 | 0,1567 | 0,826 | −2,5 % | 1/4 |
| Regresión logística | 0,1284 | 0,822 | +1,1 % | [−0,005; +0,002] | 3/6 | 0,1521 | 0,838 | +0,4 % | 2/4 |
| Gradient boosting | 0,1299 | 0,821 | −0,1 % | [−0,004; +0,004] | 3/6 | 0,1538 | 0,829 | −0,6 % | 1/4 |

### Resultados fuera de entrenamiento: lluvia

| Modelo | Selección: Brier | AUC | Mejora | IC 95 % | Años mejores | Reserva: Brier | AUC | Mejora | Años mejores |
|---|---|---|---|---|---|---|---|---|---|
| Prevalencia global | 0,1410 | 0,489 | −13,7 % | [+0,011; +0,023] | 0/6 | 0,1676 | 0,435 | −23,7 % | 0/4 |
| **Climatología (baseline)** | **0,1241** | **0,746** | — | — | — | **0,1355** | **0,785** | — | — |
| Climatología calibrada | 0,1231 | 0,747 | +0,8 % | [−0,002; +0,000] | 4/6 | 0,1368 | 0,787 | −1,0 % | 0/4 |
| Regresión logística | 0,1226 | 0,758 | +1,2 % | [−0,005; +0,001] | 4/6 | 0,1338 | 0,798 | +1,3 % | 3/4 |
| Gradient boosting | 0,1250 | 0,742 | −0,8 % | [−0,001; +0,003] | 2/6 | 0,1344 | 0,798 | +0,9 % | 3/4 |

**Lectura.** Conocer el lugar aporta mucho: la climatología reduce el Brier un 28 % en cielo despejado y un 19 % en lluvia frente a la prevalencia global, sobre la reserva. Añadir un modelo encima aporta muy poco. Ninguna alternativa cumple la regla en selección, por lo que se mantiene la climatología en los dos objetivos.

La reserva respalda esa prudencia. La climatología calibrada, la mejor en selección para cielo despejado, empeora en reserva y queda por detrás de la frecuencia sin corregir. La regresión logística es la que mejor se comporta en reserva, pero con una mejora de entre 0,4 % y 1,3 % e intervalos que incluyen el cero.

### Valor para la decisión (cielo despejado)

Porcentaje de eventos × año en que el primer destino recomendado estuvo despejado. Los empates se promedian.

| Método | Selección (78 eventos × año) | Reserva (51 eventos × año) |
|---|---|---|
| Elegir al azar | 23,8 % | 29,9 % |
| **Climatología** | **49,4 %** | **58,8 %** |
| Climatología calibrada | 49,4 % | 58,8 % |
| Regresión logística | 51,3 % | 64,7 % |
| Gradient boosting | 44,9 % | 58,8 % |
| Máximo posible (algún candidato despejado) | 85,9 % | 90,2 % |

La climatología duplica aproximadamente el resultado de elegir al azar. La ventaja de la regresión logística en la reserva equivale a unos 3 casos de 51, poca diferencia para decidir con ella.

### Errores más relevantes (climatología, reserva)

- **Cambio de condiciones en los años recientes.** En 2023–2026 aumentan a la vez los casos despejados (30,0 % frente a 23,5 % en 2017–2022) y los de lluvia (21,0 % frente a 17,0 %). La climatología los infraestima: prevé un 26,1 % y un 16,5 %. Con cuatro años no se puede determinar si es variabilidad natural o un cambio más persistente.
- **Las probabilidades intermedias son las más inciertas.** En destinos con climatología entre 30 % y 60 % el Brier de cielo despejado es 0,232, cerca del 0,25 de un reparto al 50 %; entre 0 % y 10 % es 0,039. Es sobre todo incertidumbre propia del tiempo en una fecha concreta.
- **Calibración irregular en algunos tramos.** Con probabilidad prevista del 20–30 % se despejó el 41,4 % de las veces (87 filas).
- **Más error de lluvia en los trópicos y en eventos lunares.** El Brier de lluvia es 0,160 en la zona tropical frente a 0,095 en latitudes medias, y 0,173 en lunares frente a 0,095 en solares. En los trópicos llueve el 26,7 % de las veces y se prevé el 19,1 %.
- **Casos individuales extremos.** Curitiba estuvo despejada en 3 de 4 años con una previsión del 11 %. Con cuatro observaciones por lugar no indican un fallo del lugar, sino que un año concreto puede alejarse mucho de la frecuencia. El detalle está en `worst_locations.csv`.

### Limitaciones

- Solo hay 10 años de validación. Los intervalos son aproximados y no detectar una mejora **no prueba** que no exista.
- La etiqueta procede de ERA5 con umbrales fijos; una mejora frente a ella no equivale a una mejora en la observación real.
- Se evalúa solo el componente meteorológico, no la puntuación combinada, cuyos pesos siguen pendientes de validar.
- Antes de fijar este diseño se hizo una prueba exploratoria con una regresión logística sobre 2021–2026, que se solapa con la reserva. No se ajustó ningún hiperparámetro con ella, pero la reserva no es del todo independiente de esa exploración.

**Reproducción.** `python src/ml_planning_experiment.py` regenera los resultados y la opción `--check` los compara con los guardados. El informe `experiment_report.json` registra los hashes del Gold utilizado, la versión del script y los modelos elegidos.

## 5. Decisión y uso en el producto

**Solución elegida: climatología histórica por lugar y fecha** para cielo despejado y lluvia. Cumple la regla fijada, es la opción más simple y fácil de explicar, y evita mantener un modelo sin una mejora demostrada.

**Mejora que ofrece.** Frente a no usar información del lugar, reduce el Brier entre un 19 % y un 28 % y duplica aproximadamente la probabilidad de acertar con el primer destino. Frente a lo que ya usa el producto no añade precisión: el valor de esta entrega es haber comprobado con evidencia que un modelo más complejo todavía no se justifica.

**¿Es suficiente para el MVP?** Sí para comparar destinos con antelación y comunicar la probabilidad histórica. No sirve como pronóstico del día del eclipse: esa función la cubre la capa de pronóstico operativo en los 16 días anteriores.

**Salida que consumirá el frontal.** [`data/gold/ml/query_predictions.csv`](../../data/gold/ml/query_predictions.csv), con una fila por evento × ubicación (159 en total):

| Campo | Significado |
|---|---|
| `event_id`, `event_date`, `name`, `country_code`, `candidate_type` | Evento y destino, con las mismas claves que el Gold |
| `clear_sky_probability`, `rain_probability` | Estimación entre 0 y 1: frecuencia histórica con 30 años previos |
| `clear_sky_band`, `rain_band` | Categoría para el usuario |
| `clear_sky_model`, `rain_model` | Solución que generó la estimación |

Las probabilidades coinciden, salvo redondeo, con `clear_probability` y `rain_probability` de `location_ranking.csv`, que el selector ya muestra, así que no hace falta cambiar la fuente de datos. Las novedades para el frontal definitivo son la categoría y el rango de incertidumbre.

| Categoría | Probabilidad | Cielo despejado: previsto / observado (reserva) | Lluvia: previsto / observado (reserva) |
|---|---|---|---|
| Baja | <25 % | 9,2 % / 12,3 % (366 filas) | 9,3 % / 12,4 % (477 filas) |
| Media | 25–50 % | 34,6 % / 43,7 % (158 filas) | 34,2 % / 43,8 % (121 filas) |
| Alta | ≥50 % | 74,8 % / 73,0 % (100 filas) | 66,9 % / 73,1 % (26 filas) |

**Cautelas para el usuario.**

- La cifra significa «en los últimos 30 años, este lugar estuvo despejado en esta fecha y hora con esta frecuencia». No es un pronóstico.
- Un 0 % o un 100 % solo indican que no se observó lo contrario en 30 años. Conviene mostrar junto a la cifra el intervalo de Wilson, ya calculado en `location_ranking.csv` (`clear_lower95`, `clear_upper95`) pero aún no exportado al selector.
- En la categoría media, lo observado en la reserva superó a lo previsto en unos 9 puntos, así que la cifra debe leerse como orientativa.
- El cielo despejado se define con datos de reanálisis para una celda de unos 25 km y puede diferir de lo que se vea desde un punto concreto.

**Continuidad.** La entrada es el Gold de la entrega 2 y la salida es el contrato anterior. Conviene repetir el experimento con la regla ya definida cuando haya más años disponibles. La regresión logística es la candidata a revisar, porque mejoró a la climatología en lluvia en 3 de los 4 años de la reserva.
