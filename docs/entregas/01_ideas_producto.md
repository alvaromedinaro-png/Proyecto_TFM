# Idea X - Steam Achievement Pathfinder & Optimizer

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
