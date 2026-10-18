# Datos de la exploración inicial

Material de la primera aproximación al eclipse del 2 de agosto de 2027, anterior al pipeline actual. Se conserva como antecedente y lo usan [prepare_eclipse_data.py](../../src/prepare_eclipse_data.py) y sus tests. **No forma parte del producto** y el selector no lo usa.

- `bronze/`: trayectoria y muestra meteorológica extraídas en CSV. Las respuestas originales no se conservaron. También incluye pruebas de alojamiento y de predicción.
- `silver/` y `gold/`: salidas de `python src/prepare_eclipse_data.py` y del modo `--legacy`.
- `eclipse_recommendations_2027-08-02.csv`: ranking de la exploración.
- `models/`: modelo de prueba descartado. No está versionado; la entrega 2 no requiere entrenar modelos.
