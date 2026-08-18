# Idea 1 - Steam Achievement Pathfinder & Optimizer

## Problema que resuelve
* **Qué ocurre actualmente:** Los jugadores en Steam que buscan completar sus juegos al 100% ("achievement hunters") suelen recurrir a guías externas dispersas, wikis o foros para rastrear qué logros les faltan, qué orden seguir o si existen logros perdibles (*missable achievements*). La interfaz oficial de Steam solo muestra una lista estática de logros obtenidos y bloqueados, sin contexto estratégico.
* **Por qué supone un problema:** El usuario pierde tiempo consultando múltiples fuentes externas para planificar su partida. Además, corre el riesgo de arruinar decenas de horas de juego por saltarse un logro perdible durante una misión específica.
* **Qué decisión, proceso o situación podría mejorar con datos:** Se mejoraría la planificación de partidas y la toma de decisiones durante el juego mediante la organización automática de logros según rutas óptimas, estimación de tiempo/dificultad y alertas de elecciones irreversibles.
* **Por qué no es simplemente un ejercicio técnico:** Resuelve una necesidad real de optimización de tiempo y frustración en una comunidad muy activa. La complejidad radica en estructurar grafos de requisitos de logros y modelar datos de progresión y comportamiento del jugador.

## Motivación para realizar el proyecto
* **Relevancia personal y empresarial:** La industria del videojuego mueve millones de usuarios orientados a métricas de retención y completismo. Existe una comunidad masiva (*completionists*) dedicada al trofeo/logro.
* **Interés profesional o técnico:** Permite trabajar con arquitecturas de grafos de decisión, procesamiento de lenguaje natural (NLP) para análisis de guías, algoritmos de recomendación/secuenciación y la integración con la Web API pública de Steam.
* **Utilidad potencial:** Simplifica radicalmente la experiencia de completismo para jugadores entusiastas sin necesidad de *spoilers* innecesarios.
* **Existencia de datos:** Existen APIs públicas oficiales (Steam Web API) que ofrecen datos en tiempo real de perfiles, biblioteca de juegos, estado de logros por usuario y porcentajes globales de obtención. También es posible extraer información contextual mediante web scraping o datasets de plataformas como *TrueSteamAchievements* o *HowLongToBeat*.

## A quién impacta
* **Jugadores completistas (*Achievement Hunters*):** Usuarios enfocados en desbloquear el 100% de sus títulos de forma eficiente.
* **Jugadores con poco tiempo libre:** Personas que desean maximizar el progreso de sus juegos sin perder tiempo repitiendo partidas por fallos de planificación.
* **Creadores de contenido / Guiadores:** Editores que analizan rutas de trofeos y pueden usar esta analítica para crear contenido optimizado.

## Por qué tiene valor
* **Ahorro de tiempo:** Evita tener que repetir un juego desde el inicio por haber pasado por alto un logro en una decisión irreversible.
* **Mejora de planificación:** Genera "rutas de completado" personalizadas y secuenciales según el punto exacto de la historia donde se encuentra el jugador.
* **Detección de riesgos:** Alerta explícitamente sobre logros perdibles o decisiones excluyentes (*missables*) antes de avanzar en la trama.
* **Automatización de tareas:** Agrupa automáticamente la información contextual dispersa en foros y guías en una única vista inteligente analizada por datos.
* **Mayor transparencia y experiencia de usuario:** Ofrece visibilidad real sobre la dificultad y tiempo restante estimado para el 100% adaptado al ritmo del usuario.


# Idea 2 - Asistente Personalizado de Control Glucémico e Insulina

## Problema que resuelve
* **Qué ocurre actualmente:** Muchas personas con diabetes ajustan la insulina de forma reactiva, basándose en estimaciones generales y en su experiencia, sin una predicción clara de cómo les afectarán alimentos concretos en cada momento.
* **Por qué supone un problema:** Esto puede provocar picos o bajadas de glucosa evitables, mayor carga mental diaria y, en algunos casos, un uso de insulina menos eficiente del necesario.
* **Qué decisión, proceso o situación podría mejorar con datos:** Se mejoraría la decisión sobre dosis y momento de administración al predecir cuánto podría subir la glucosa tras ciertos alimentos y cómo respondería cada usuario según su histórico.
* **Por qué no es simplemente un ejercicio técnico:** Tiene impacto directo en salud y calidad de vida. El valor está en ofrecer recomendaciones personalizadas, comprensibles y seguras para apoyar el autocuidado diario.

## Motivación para realizar el proyecto
* **Relevancia personal y social:** La diabetes afecta a millones de personas y requiere decisiones constantes cada día. Reducir incertidumbre y mejorar el control glucémico tiene un gran impacto individual y sanitario.
* **Interés profesional o técnico:** Permite trabajar con series temporales, modelos predictivos personalizados, aprendizaje continuo por usuario y sistemas de recomendación explicables.
* **Utilidad potencial:** Ayuda a minimizar el uso de insulina cuando sea posible y seguro, manteniendo el control glucémico mediante recomendaciones adaptadas al contexto real del usuario.
* **Existencia de datos:** Se pueden integrar datos de glucómetros/CGM, registro de comidas, si fuera posible, actividad física, sueño, dosis previas y eventos diarios para construir perfiles dinámicos por persona.

## A quién impacta
* **Personas con diabetes insulinodependiente:** Usuarios que necesitan tomar decisiones frecuentes sobre alimentación, glucosa e insulina.
* **Familiares y cuidadores:** Pueden obtener mayor visibilidad del patrón glucémico y apoyo en decisiones rutinarias.
* **Profesionales sanitarios:** Tendrían acceso a métricas y tendencias útiles para seguimiento más personalizado entre consultas.

## Por qué tiene valor
* **Personalización real:** No aplica reglas genéricas; ajusta recomendaciones al comportamiento glucémico histórico de cada usuario.
* **Prevención de picos e hipoglucemias:** Anticipa respuestas probables ante comidas y contexto diario para actuar antes de que ocurra el descontrol.
* **Optimización del uso de insulina:** Busca reducir dosis innecesarias manteniendo objetivos de control glucémico definidos.
* **Aprendizaje continuo:** Mejora sus predicciones conforme recopila más datos del usuario y detecta cambios en su patrón metabólico.
* **Mejor experiencia de autocuidado:** Reduce carga cognitiva al convertir datos complejos en recomendaciones claras y accionables.


# Idea 3 - Planificador Geoespacial de Observación de Eclipses

## Problema que resuelve
* **Qué ocurre actualmente:** Muchas personas interesadas en observar eclipses solares o lunares consultan mapas, artículos, foros y herramientas dispersas para saber desde qué ubicaciones se verá mejor un fenómeno concreto, con qué recorrido y con qué probabilidad de cielo despejado.
* **Por qué supone un problema:** La información relevante suele estar fragmentada y no siempre es fácil traducirla a una decisión práctica: dónde ir, cuándo desplazarse, cuánto esperar de visibilidad o qué punto ofrece la mejor combinación entre duración, cobertura y meteorología.
* **Qué decisión, proceso o situación podría mejorar con datos:** Se mejoraría la planificación de viajes y observaciones astronómicas mediante recomendaciones de ubicaciones óptimas para ver eclipses futuros, por ejemplo el de 2027, combinando trazado de la sombra, duración del evento, accesibilidad y condiciones climáticas históricas.
* **Por qué no es simplemente un ejercicio técnico:** Tiene sentido real para aficionados, divulgadores, fotógrafos y planificadores de eventos astronómicos, porque ayuda a tomar una decisión concreta con impacto directo en la experiencia final de observación.

## Motivación para realizar el proyecto
* **Relevancia social y educativa:** Los eclipses generan gran interés público y son oportunidades de divulgación científica, turismo astronómico y aprendizaje en torno a la observación del cielo.
* **Interés profesional o técnico:** Permite trabajar con geolocalización, análisis espacial, series temporales meteorológicas, visualización de mapas y sistemas de recomendación basados en varios criterios.
* **Utilidad potencial:** Puede servir para orientar a personas que quieren planificar con antelación desplazamientos para ver eclipses en condiciones óptimas, evitando decisiones improvisadas o poco informadas.
* **Existencia de datos:** Existen efemérides astronómicas, trayectorias de eclipses, datos de meteorología histórica, altitud, accesibilidad y mapas que permitirían construir un análisis viable y visualmente atractivo.

## A quién impacta
* **Aficionados a la astronomía:** Personas que quieren ver eclipses desde un punto con alta probabilidad de buena visibilidad.
* **Fotógrafos y creadores de contenido:** Usuarios que necesitan elegir ubicaciones con mejores condiciones de luz, encuadre y duración del fenómeno.
* **Divulgadores, asociaciones y grupos astronómicos:** Organizaciones que preparan salidas, encuentros o actividades alrededor de eventos astronómicos.
* **Turismo local y planificadores de eventos:** Actores que pueden aprovechar la previsión de afluencia y la localización de puntos óptimos para organizar mejor servicios y desplazamientos.

## Por qué tiene valor
* **Mejora de planificación:** Ayuda a elegir con más criterio el mejor punto de observación antes de que ocurra el eclipse.
* **Ahorro de tiempo y desplazamientos:** Reduce la necesidad de comparar manualmente múltiples fuentes para decidir una ubicación.
* **Reducción de riesgo de mala observación:** Permite evitar zonas con baja cobertura, mala meteorología histórica o mala accesibilidad.
* **Mayor transparencia y experiencia de usuario:** Resume información compleja en una recomendación clara y comprensible.
* **Apoyo a la decisión profesional o de divulgación:** Ofrece una base útil para organizar salidas, rutas y materiales divulgativos en torno a eclipses concretos.


