"use strict";

(() => {
  const data = window.ECLIPSE_PLANNER_DATA;
  const world = window.ECLIPSE_WORLD_MAP;
  const byId = (id) => document.getElementById(id);
  const familySelect = byId("family-selector");
  const eventSelect = byId("event-selector");
  const countrySelect = byId("country-selector");
  const locationContainer = byId("locations");
  const showMore = byId("show-more");
  const number = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 });
  const collator = new Intl.Collator("es", { sensitivity: "base" });
  const dateFormatter = new Intl.DateTimeFormat("es-ES", {
    day: "2-digit", month: "long", year: "numeric", timeZone: "UTC"
  });
  const dateTimeFormatter = new Intl.DateTimeFormat("es-ES", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC"
  });
  const phaseNames = {
    total: "Total", annular: "Anular", partial: "Parcial", hybrid: "Híbrido",
    penumbral: "Penumbral", none: "No visible"
  };
  const unknownCountry = "__unknown__";
  // El umbral se exporta con los datos para que interfaz y documentación coincidan.
  const minSolarObscuration = data && typeof data.solar_obscuration_threshold_pct === "number" && Number.isFinite(data.solar_obscuration_threshold_pct) ? data.solar_obscuration_threshold_pct : 70;
  const mapWidth = 960;
  const mapHeight = 480;
  let events = [];
  let selectedEvent = null;
  let locations = [];
  let countries = [];
  let expanded = false;
  let selectedLocation = null;
  let mapSvg = null;
  let mapZoom = 1;
  let mapCenter = [mapWidth / 2, mapHeight / 2];
  const mapMarkers = new Map();

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  }

  function svgNode(tag, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, String(value)));
    return element;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function formatted(value, suffix = "") {
    return finite(value) ? `${number.format(value)}${suffix}` : "No disponible";
  }

  function dateValue(value) {
    if (typeof value !== "string" || !value) return null;
    const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function readableDate(value) {
    const date = dateValue(value);
    return date ? dateFormatter.format(date) : "Fecha no disponible";
  }

  // Ventana [hoy, hoy + años) en UTC, igual que planning_window en Python (29/02 pasa a 28/02).
  function currentWindow(years) {
    const today = new Date().toISOString().slice(0, 10);
    const [year, month, day] = today.split("-");
    const endDay = month === "02" && day === "29" ? "28" : day;
    return [today, `${Number(year) + years}-${month}-${endDay}`];
  }

  function peakTime(value) {
    const date = dateValue(value);
    return date ? `${dateTimeFormatter.format(date)} UTC` : "No disponible";
  }

  function duration(value) {
    if (!finite(value) || value < 0) return "No disponible";
    const seconds = Math.round(value);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    return [hours ? `${hours} h` : "", minutes ? `${minutes} min` : "", rest || (!hours && !minutes) ? `${rest} s` : ""].filter(Boolean).join(" ");
  }

  function stat(label, value, note, className = "") {
    const item = node("div", `stat ${className}`.trim());
    item.append(node("dt", "", label));
    const content = node("dd", value === "No disponible" ? "unavailable" : "", value);
    if (note) content.append(node("small", "", note));
    item.append(content);
    return item;
  }

  function eventTitle(event) {
    const family = event.event_family === "lunar" ? "lunar" : "solar";
    const phase = phaseNames[event.eclipse_type] || event.eclipse_type || "";
    return `Eclipse ${family} ${phase.toLocaleLowerCase("es")}`.trim();
  }

  function eventLabel(event) {
    if (typeof event.label === "string" && event.label.trim()) return event.label;
    const date = dateValue(event.event_date);
    const short = date ? new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(date).replaceAll("/", "-") : "Sin fecha";
    return `${short} · ${eventTitle(event)}`;
  }

  function sampleLabel(value) {
    return finite(value) && value > 0 ? `${number.format(value)} años válidos` : "Muestra no disponible";
  }

  function countryCode(value) {
    return typeof value === "string" && value.trim() ? value.trim().toUpperCase() : unknownCountry;
  }

  function locationCountry(location) {
    return typeof location.country_name === "string" && location.country_name.trim() ? location.country_name : "País sin identificar";
  }

  function eligibleLocation(location) {
    if (selectedEvent.event_family === "solar") return finite(location.obscuration_pct) && location.obscuration_pct >= minSolarObscuration;
    return location.local_phase !== "none" && (!finite(location.body_altitude_deg) || location.body_altitude_deg > 0);
  }

  function locationSort(a, b) {
    const metric = selectedEvent.event_family === "solar" ? "obscuration_pct" : "body_altitude_deg";
    const delta = (finite(b[metric]) ? b[metric] : -Infinity) - (finite(a[metric]) ? a[metric] : -Infinity);
    return (Number.isNaN(delta) ? 0 : delta) || (finite(a.rank) ? a.rank : Infinity) - (finite(b.rank) ? b.rank : Infinity) || (finite(b.phase_duration_s) ? b.phase_duration_s : 0) - (finite(a.phase_duration_s) ? a.phase_duration_s : 0) || collator.compare(a.name || "", b.name || "");
  }

  function prepareCountries() {
    locations = (Array.isArray(selectedEvent.locations) ? selectedEvent.locations : []).map((location, index) => ({
      ...location, _key: `${selectedEvent.event_id}-${index}`, _country: countryCode(location.country_code)
    })).filter(eligibleLocation).sort(locationSort);
    const grouped = new Map();
    (Array.isArray(selectedEvent.countries) ? selectedEvent.countries : []).forEach((country) => {
      const code = countryCode(country.country_code);
      grouped.set(code, { ...country, country_code: code, country_name: locationCountry(country) });
    });
    locations.forEach((location) => {
      if (!grouped.has(location._country)) grouped.set(location._country, {
        country_code: location._country, country_name: locationCountry(location), max_obscuration_pct: null, max_body_altitude_deg: null
      });
      const country = grouped.get(location._country);
      if (finite(location.obscuration_pct)) country.max_obscuration_pct = Math.max(finite(country.max_obscuration_pct) ? country.max_obscuration_pct : -Infinity, location.obscuration_pct);
      if (finite(location.body_altitude_deg)) country.max_body_altitude_deg = Math.max(finite(country.max_body_altitude_deg) ? country.max_body_altitude_deg : -Infinity, location.body_altitude_deg);
    });
    countries = [...grouped.values()].filter((country) => selectedEvent.event_family === "lunar" || (finite(country.max_obscuration_pct) && country.max_obscuration_pct >= minSolarObscuration));
    const metric = selectedEvent.event_family === "solar" ? "max_obscuration_pct" : "max_body_altitude_deg";
    countries.sort((a, b) => (finite(b[metric]) ? b[metric] : -Infinity) - (finite(a[metric]) ? a[metric] : -Infinity) || collator.compare(a.country_name, b.country_name));
    countrySelect.replaceChildren();
    const all = node("option", "", "Todos los países disponibles");
    all.value = "all";
    countrySelect.append(all);
    countries.forEach((country) => {
      const detail = selectedEvent.event_family === "solar" ? ` · ${formatted(country.max_obscuration_pct, " %")}` : finite(country.max_body_altitude_deg) ? ` · Luna a ${formatted(country.max_body_altitude_deg, "°")}` : "";
      const option = node("option", "", `${country.country_name}${detail}`);
      option.value = country.country_code;
      countrySelect.append(option);
    });
    countrySelect.disabled = countries.length === 0;
    countrySelect.value = "all";
    byId("country-note").textContent = selectedEvent.event_family === "solar"
      ? `Países ordenados por la mayor ocultación solar entre sus zonas analizadas. Se muestran países y zonas con al menos un ${formatted(minSolarObscuration, " %")} de disco solar oculto; el porcentaje varía dentro de cada país.`
      : "Países y zonas ordenados por la altura de la Luna en el máximo global. En los eclipses lunares no se utiliza un porcentaje de ocultación solar.";
  }

  function hasClimate(location) {
    if (location.climate_status === "not_prepared") return false;
    return [location.clear_probability_pct, location.rain_probability_pct, location.mean_cloud_cover_pct, location.mean_temperature_c, location.mean_wind_speed_kmh].some(finite);
  }

  function dailyDetails(location) {
    const days = Array.isArray(location.climate_by_day) ? location.climate_by_day.slice().sort((a, b) => a.day_offset - b.day_offset) : [];
    if (!days.length) return null;
    const details = node("details");
    details.append(node("summary", "", "Clima en los días cercanos"));
    const wrapper = node("div", "table-wrap");
    const table = node("table", "climate-table");
    const caption = node("caption", "", "Climatología en la hora de referencia del eclipse");
    caption.style.textAlign = "left";
    caption.style.marginBottom = "5px";
    const head = node("thead");
    const headerRow = node("tr");
    ["Día", "Temp.", "Nubes", "Despejado", "Lluvia"].forEach((label) => {
      const cell = node("th", "", label);
      cell.scope = "col";
      headerRow.append(cell);
    });
    head.append(headerRow);
    const body = node("tbody");
    days.forEach((day) => {
      const row = node("tr", day.day_offset === 0 ? "eclipse-day" : "");
      const offset = day.day_offset;
      const label = offset === 0 ? "Eclipse" : finite(offset) ? `${offset > 0 ? "+" : ""}${offset} ${Math.abs(offset) === 1 ? "día" : "días"}` : "Sin referencia";
      const heading = node("th", "", label);
      heading.scope = "row";
      row.append(heading);
      [formatted(day.mean_temperature_c, " °C"), formatted(day.mean_cloud_cover_pct, " %"), formatted(day.clear_probability_pct, " %"), formatted(day.rain_probability_pct, " %")].forEach((value) => row.append(node("td", "", value === "No disponible" ? "—" : value)));
      row.title = `Cielo: ${sampleLabel(day.valid_cloud_years)}; lluvia: ${sampleLabel(day.valid_rain_years)}.`;
      body.append(row);
    });
    table.append(caption, head, body);
    wrapper.append(table);
    details.append(wrapper, node("p", "table-note", "Despejado y lluvia: frecuencias históricas. Nubes: porcentaje medio de cielo cubierto. —: dato no disponible."));
    return details;
  }

  function locationCard(location, index, event) {
    const card = node("article", `location-card${selectedLocation === location._key ? " selected" : ""}`);
    card.id = `location-${location._key}`;
    card.dataset.locationKey = location._key;
    card.tabIndex = -1;
    const heading = node("div", "card-heading");
    heading.append(node("span", "rank", String(index + 1)));
    const title = node("div");
    const name = node("h4", "", location.name || "Ubicación sin nombre");
    name.id = `${card.id}-title`;
    card.setAttribute("aria-labelledby", name.id);
    title.append(name, node("span", "location-country", locationCountry(location)));
    if (finite(location.latitude) && finite(location.longitude)) title.append(node("span", "coordinate", `${formatted(location.latitude, "°")}, ${formatted(location.longitude, "°")}`));
    heading.append(title);
    card.append(heading);
    const localPhase = phaseNames[location.local_phase] || location.local_phase;
    if (localPhase) card.append(node("span", "phase-label", `Fase visible: ${localPhase}`));
    const astronomy = node("dl", "astronomy-stats");
    if (event.event_family === "solar") astronomy.append(stat("Disco solar oculto", formatted(location.obscuration_pct, " %"), "En el máximo local"));
    else {
      const visibility = !finite(location.body_altitude_deg) ? "No disponible" : location.body_altitude_deg > 0 ? "Sobre el horizonte" : location.body_altitude_deg === 0 ? "En el horizonte" : "Bajo el horizonte";
      astronomy.append(stat("Visibilidad", visibility, "En el máximo global lunar"));
    }
    const durationLabel = location.duration_scope === "global_lunar_phase" ? "Duración de fase global" : location.duration_scope === "local_central" ? "Duración de fase central" : "Duración visible local";
    const durationNote = location.duration_scope === "global_lunar_phase" ? "No garantiza que toda la fase sea visible desde aquí." : location.duration_scope === "local_central" ? "Totalidad o fase anular local" : "Intervalo visible del eclipse";
    const durationOutside = !finite(location.phase_duration_s) && location.duration_complete === false;
    astronomy.append(stat(durationLabel, duration(location.phase_duration_s), durationOutside ? "La fase sale del intervalo de cálculo de los elementos NASA" : durationNote));
    astronomy.append(stat(event.event_family === "lunar" ? "Máximo global" : "Máximo local", peakTime(location.peak_time_utc), event.event_family === "lunar" ? "Hora del máximo global lunar" : "Hora aproximada"));
    astronomy.append(stat("Altura sobre el horizonte", formatted(location.body_altitude_deg, "°"), event.event_family === "lunar" ? "Luna en el máximo global" : "Sol en el máximo local"));
    card.append(astronomy);
    if (!hasClimate(location)) {
      const pending = node("p", "climate-pending");
      pending.append(node("strong", "", "Climatología pendiente"), document.createTextNode("La geometría del eclipse está calculada para esta zona; sus datos meteorológicos aún no están preparados."));
      card.append(pending);
      return card;
    }
    card.append(node("h5", "climate-heading", "Climatología en la hora del eclipse"));
    const climate = node("dl", "climate-stats");
    climate.append(stat("Cielo despejado", formatted(location.clear_probability_pct, " %"), sampleLabel(location.valid_cloud_years)));
    climate.append(stat("Frecuencia de lluvia", formatted(location.rain_probability_pct, " %"), sampleLabel(location.valid_rain_years), "rain-stat"));
    card.append(climate);
    const secondary = node("div", "weather-secondary");
    [["Nubosidad", location.mean_cloud_cover_pct, " %"], ["Temperatura", location.mean_temperature_c, " °C"], ["Viento", location.mean_wind_speed_kmh, " km/h"]].forEach(([label, value, unit]) => {
      const item = node("span", "", `${label}: `);
      item.append(node("strong", "", formatted(value, unit)));
      secondary.append(item);
    });
    card.append(secondary);
    if ([location.valid_cloud_years, location.valid_rain_years].some((value) => finite(value) && value > 0 && value < 15)) card.append(node("p", "provisional-note", "Climatología provisional: cobertura histórica limitada."));
    else if (location.climate_status === "partial") card.append(node("p", "climate-partial", "Parte de los datos meteorológicos todavía no está disponible."));
    const details = dailyDetails(location);
    if (details) card.append(details);
    return card;
  }

  function visibleLocations() {
    return countrySelect.value === "all" ? locations : locations.filter((location) => location._country === countrySelect.value);
  }

  function renderLocations() {
    locationContainer.replaceChildren();
    const filtered = visibleLocations();
    const empty = byId("locations-empty");
    empty.hidden = filtered.length > 0;
    const selectedCountry = countries.find((country) => country.country_code === countrySelect.value);
    byId("locations-title").textContent = selectedCountry ? `Zonas de ${selectedCountry.country_name}` : "Ubicaciones para observar";
    if (!filtered.length) {
      const rawLocations = Array.isArray(selectedEvent.locations) ? selectedEvent.locations : [];
      const thresholdEmpty = selectedEvent.event_family === "solar" && rawLocations.length > 0;
      empty.replaceChildren(
        node("strong", "", selectedCountry ? "Zonas pendientes de preparar" : thresholdEmpty ? `Sin zonas que alcancen el ${formatted(minSolarObscuration, " %")}` : "Ubicaciones pendientes de preparar"),
        node("p", "", selectedCountry ? "Este país figura entre los resultados astronómicos, pero sus zonas todavía no están incluidas en esta descarga." : thresholdEmpty ? "Ninguna de las ubicaciones guardadas alcanza el umbral de ocultación solar de esta vista." : "El eclipse está en el catálogo. Sus ubicaciones y datos meteorológicos todavía no están disponibles en esta descarga.")
      );
    }
    const shown = expanded ? filtered : filtered.slice(0, 3);
    shown.forEach((location, index) => locationContainer.append(locationCard(location, index, selectedEvent)));
    showMore.hidden = filtered.length <= 3;
    showMore.textContent = expanded ? "Mostrar las tres primeras" : `Ver las ${filtered.length} ubicaciones`;
    showMore.setAttribute("aria-expanded", String(expanded));
    showMore.setAttribute("aria-controls", "locations");
    const preparedCount = filtered.filter(hasClimate).length;
    const order = selectedEvent.event_family === "solar" ? "Ordenadas por disco solar oculto." : "Ordenadas por altura de la Luna.";
    byId("locations-note").textContent = filtered.length ? `${filtered.length} zonas analizadas · ${preparedCount} con climatología. ${order}` : "Los resultados aparecerán cuando estén preparados.";
  }

  function project(longitude, latitude) {
    return [(longitude + 180) * mapWidth / 360, (90 - latitude) * mapHeight / 180];
  }

  function geometryPath(geometry) {
    const polygons = geometry && geometry.type === "Polygon" ? [geometry.coordinates] : geometry && geometry.type === "MultiPolygon" ? geometry.coordinates : [];
    return polygons.map((polygon) => polygon.map((ring) => ring.filter((point) => Array.isArray(point) && finite(point[0]) && finite(point[1])).map((point, index) => {
      const [x, y] = project(point[0], point[1]);
      return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ") + " Z").join(" ")).join(" ");
  }

  function activateOnKeyboard(element, action) {
    element.addEventListener("click", action);
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        action();
      }
    });
  }

  function updateMapView() {
    if (!mapSvg) return;
    const width = mapWidth / mapZoom;
    const height = mapHeight / mapZoom;
    mapCenter[0] = Math.max(width / 2, Math.min(mapWidth - width / 2, mapCenter[0]));
    mapCenter[1] = Math.max(height / 2, Math.min(mapHeight - height / 2, mapCenter[1]));
    mapSvg.setAttribute("viewBox", `${mapCenter[0] - width / 2} ${mapCenter[1] - height / 2} ${width} ${height}`);
    mapMarkers.forEach((entry) => {
      entry.dot.setAttribute("r", 4.4 / mapZoom);
      entry.hit.setAttribute("r", 13 / mapZoom);
      entry.focus.setAttribute("r", 7.5 / mapZoom);
      const strokeWidth = entry.group.classList.contains("selected") ? 2 : entry.group.classList.contains("pending") ? 1.6 : 1.2;
      entry.dot.setAttribute("stroke-width", strokeWidth / mapZoom);
      entry.focus.setAttribute("stroke-width", 1.7 / mapZoom);
    });
    byId("map-zoom-in").disabled = mapZoom >= 8;
    byId("map-zoom-out").disabled = mapZoom <= 1;
  }

  function focusCountry(code) {
    const points = locations.filter((location) => location._country === code && finite(location.latitude) && finite(location.longitude)).map((location) => project(location.longitude, location.latitude));
    if (!points.length || code === "all") {
      mapZoom = 1;
      mapCenter = [mapWidth / 2, mapHeight / 2];
    } else {
      const xs = points.map((point) => point[0]);
      const ys = points.map((point) => point[1]);
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      mapCenter = [(minX + maxX) / 2, (minY + maxY) / 2];
      mapZoom = Math.min(6, Math.max(1, Math.min(mapWidth / (maxX - minX + 110), mapHeight / (maxY - minY + 70))));
    }
    updateMapView();
  }

  function updateMapSelection() {
    if (mapSvg) {
      mapSvg.querySelectorAll(".map-country[data-country]").forEach((path) => {
        const selected = path.dataset.country === countrySelect.value;
        path.classList.toggle("selected", selected);
        if (path.classList.contains("available")) path.setAttribute("aria-pressed", String(selected));
      });
      mapMarkers.forEach(({ group }, key) => {
        const location = locations.find((item) => item._key === key);
        group.hidden = countrySelect.value !== "all" && location._country !== countrySelect.value;
        group.style.display = group.hidden ? "none" : "";
        const selected = selectedLocation === key;
        group.classList.toggle("selected", selected);
        group.setAttribute("aria-pressed", String(selected));
      });
    }
    updateMapView();
    const country = countries.find((item) => item.country_code === countrySelect.value);
    const location = locations.find((item) => item._key === selectedLocation);
    byId("map-selection").textContent = location ? `${location.name} · ${locationCountry(location)}${selectedEvent.event_family === "solar" ? ` · ${formatted(location.obscuration_pct, " %")} de disco solar oculto` : ` · Luna a ${formatted(location.body_altitude_deg, "°")}`}` : country ? `${country.country_name} · ${visibleLocations().length} zonas disponibles` : `${countries.length} países disponibles · ${locations.length} zonas analizadas`;
  }

  function chooseCountry(code, zoom = true) {
    if (![...countrySelect.options].some((option) => option.value === code)) return;
    countrySelect.value = code;
    expanded = false;
    selectedLocation = null;
    renderLocations();
    updateMapSelection();
    if (zoom) focusCountry(code);
  }

  function chooseLocation(location) {
    chooseCountry(location._country, false);
    selectedLocation = location._key;
    const index = visibleLocations().findIndex((item) => item._key === selectedLocation);
    expanded = index >= 3;
    renderLocations();
    updateMapSelection();
    const card = byId(`location-${location._key}`);
    if (card) {
      card.focus({ preventScroll: true });
      card.scrollIntoView({ block: "nearest", behavior: "auto" });
    }
  }

  function renderMap() {
    const container = byId("map-container");
    container.replaceChildren();
    mapMarkers.clear();
    mapSvg = null;
    mapZoom = 1;
    mapCenter = [mapWidth / 2, mapHeight / 2];
    const mapAvailable = world && world.type === "FeatureCollection" && Array.isArray(world.features) && world.features.length;
    ["map-zoom-in", "map-zoom-out", "map-reset"].forEach((id) => { byId(id).disabled = !mapAvailable; });
    if (!mapAvailable) {
      const fallback = node("div", "map-unavailable");
      fallback.append(node("strong", "", "Mapa local no disponible"), node("p", "", "Puedes explorar los países y las zonas desde el selector y las tarjetas."));
      container.append(fallback);
      updateMapSelection();
      return;
    }
    mapSvg = svgNode("svg", { viewBox: `0 0 ${mapWidth} ${mapHeight}`, class: "world-map", role: "group", "aria-label": "Mapa de países y zonas analizadas para el eclipse seleccionado" });
    const graticule = svgNode("g", { class: "map-graticule", "aria-hidden": "true" });
    for (let longitude = -120; longitude <= 120; longitude += 60) {
      const [x] = project(longitude, 0);
      graticule.append(svgNode("path", { d: `M${x},0 L${x},${mapHeight}` }));
    }
    [-60, -30, 0, 30, 60].forEach((latitude) => {
      const [, y] = project(0, latitude);
      graticule.append(svgNode("path", { d: `M0,${y} L${mapWidth},${y}` }));
    });
    mapSvg.append(graticule);
    const outlines = svgNode("g");
    world.features.forEach((feature) => {
      if (!feature || !feature.geometry) return;
      const properties = feature.properties || {};
      const code = countryCode(properties.country_code);
      const country = countries.find((item) => item.country_code === code);
      const path = svgNode("path", { d: geometryPath(feature.geometry), class: `map-country${country ? " available" : ""}`, "fill-rule": "evenodd" });
      path.dataset.country = code;
      const title = svgNode("title");
      title.textContent = country ? `${country.country_name}${selectedEvent.event_family === "solar" ? `: hasta ${formatted(country.max_obscuration_pct, " %")} de disco solar oculto en las zonas analizadas` : ": zonas visibles en el máximo global lunar"}` : properties.country_name || "Territorio";
      path.append(title);
      if (country) {
        path.setAttribute("role", "button");
        path.setAttribute("tabindex", "0");
        path.setAttribute("aria-label", `Seleccionar ${title.textContent}`);
        path.setAttribute("aria-pressed", "false");
        activateOnKeyboard(path, () => chooseCountry(code));
      }
      outlines.append(path);
    });
    mapSvg.append(outlines);
    const markers = svgNode("g");
    locations.forEach((location) => {
      if (!finite(location.latitude) || !finite(location.longitude)) return;
      const [x, y] = project(location.longitude, location.latitude);
      const label = `${location.name || "Zona"}, ${locationCountry(location)}${selectedEvent.event_family === "solar" ? `: ${formatted(location.obscuration_pct, " %")} de disco solar oculto` : `: Luna a ${formatted(location.body_altitude_deg, "°")}`}${hasClimate(location) ? "" : ". Climatología pendiente"}`;
      const group = svgNode("g", { class: `map-marker${hasClimate(location) ? "" : " pending"}`, transform: `translate(${x},${y})`, role: "button", tabindex: "0", "aria-label": `Ver ${label}`, "aria-pressed": "false" });
      group.dataset.locationKey = location._key;
      const title = svgNode("title");
      title.textContent = label;
      const hit = svgNode("circle", { class: "marker-hit", r: 13 });
      const focus = svgNode("circle", { class: "marker-focus", r: 7.5 });
      const dot = svgNode("circle", { class: "marker-dot", r: 4.4 });
      group.append(title, hit, focus, dot);
      activateOnKeyboard(group, () => chooseLocation(location));
      mapMarkers.set(location._key, { group, hit, focus, dot });
      markers.append(group);
    });
    mapSvg.append(markers);
    container.append(mapSvg);
    updateMapSelection();
    updateMapView();
  }

  function renderEvent() {
    selectedEvent = events.find((event) => event.event_id === eventSelect.value);
    byId("event-content").hidden = !selectedEvent;
    if (!selectedEvent) {
      countrySelect.replaceChildren(node("option", "", "Selecciona un eclipse"));
      countrySelect.disabled = true;
      return;
    }
    expanded = false;
    selectedLocation = null;
    byId("event-date").textContent = readableDate(selectedEvent.event_date);
    byId("event-title").textContent = eventTitle(selectedEvent);
    const status = ["ready", "partial", "pending"].includes(selectedEvent.status) ? selectedEvent.status : "pending";
    const badge = byId("event-status");
    badge.className = `status-badge ${status}`;
    badge.textContent = { ready: "Datos preparados", partial: "Datos parciales", pending: "Pendiente" }[status];
    const notes = [];
    if (selectedEvent.event_family === "lunar") notes.push("La altura de la Luna se calcula en el máximo global. La duración publicada corresponde a la fase lunar global.");
    else notes.push("La ocultación y la duración corresponden a cada ubicación; la fase visible puede variar entre destinos.");
    if (status === "partial") notes.push("Parte de la climatología está pendiente de preparar.");
    if (status === "pending") notes.push("El evento está incluido en el catálogo, con su preparación pendiente.");
    if (typeof selectedEvent.ranking_basis_warning === "string") notes.push(selectedEvent.ranking_basis_warning);
    byId("event-note").textContent = notes.join(" ");
    const rawForecast = selectedEvent.forecast_status;
    const forecast = typeof rawForecast === "string" ? rawForecast : rawForecast && typeof rawForecast.status === "string" ? rawForecast.status : "unavailable";
    const forecastNote = byId("forecast-note");
    if (["available", "partially_available"].includes(forecast)) forecastNote.textContent = "Hay un pronóstico guardado para parte o toda la ventana de este evento. Los porcentajes mostrados en estas tarjetas son históricos; revisa la fecha de preparación antes de planificar.";
    else if (forecast === "stale") forecastNote.textContent = "El pronóstico guardado ha quedado desactualizado. Las tarjetas muestran la climatología histórica.";
    else forecastNote.textContent = "No hay un pronóstico operativo disponible en estos datos. Para estas fechas se utiliza la climatología histórica.";
    prepareCountries();
    renderLocations();
    renderMap();
  }

  function fillEventOptions() {
    const previous = eventSelect.value;
    const filtered = events.filter((event) => familySelect.value === "all" || event.event_family === familySelect.value);
    eventSelect.replaceChildren();
    filtered.forEach((event) => {
      const option = node("option", "", eventLabel(event));
      option.value = event.event_id;
      eventSelect.append(option);
    });
    eventSelect.disabled = filtered.length === 0;
    if (filtered.some((event) => event.event_id === previous)) eventSelect.value = previous;
    if (!filtered.length) eventSelect.append(node("option", "", "No hay eclipses de este tipo en el catálogo"));
    renderEvent();
  }

  function initialize() {
    if (!data || data.schema_version !== 1 || !Array.isArray(data.events)) {
      familySelect.disabled = true;
      eventSelect.disabled = true;
      eventSelect.append(node("option", "", "Catálogo no disponible"));
      const error = byId("load-error");
      error.hidden = false;
      error.append(node("strong", "", "Falta el catálogo local"), node("p", "", "Prepara los datos con el pipeline y vuelve a abrir esta página. La aplicación necesita el archivo data/gold/planner_data.js junto al proyecto."));
      return;
    }
    // La ventana se calcula con la fecha actual: los eclipses pasados desaparecen y los del
    // margen exportado entran como pendientes sin volver a ejecutar el pipeline.
    const years = data.planning_start && data.planning_end_exclusive
      ? Number(data.planning_end_exclusive.slice(0, 4)) - Number(data.planning_start.slice(0, 4)) : 3;
    const [windowStart, windowEnd] = currentWindow(years > 0 ? years : 3);
    events = data.events
      .filter((event) => event && typeof event.event_id === "string" && typeof event.event_date === "string")
      .filter((event) => event.event_date >= windowStart && event.event_date < windowEnd)
      .sort((a, b) => a.event_date.localeCompare(b.event_date) || a.event_id.localeCompare(b.event_id));
    const summary = byId("catalog-summary");
    const total = node("span");
    total.append(node("strong", "", String(events.length)), document.createTextNode(" eclipses en el catálogo"));
    summary.append(total);
    const lastDay = dateValue(windowEnd);
    if (lastDay) lastDay.setUTCDate(lastDay.getUTCDate() - 1);
    summary.append(node("span", "", `${readableDate(windowStart)} — ${lastDay ? dateFormatter.format(lastDay) : "fin no disponible"}`));
    const catalogEnd = data.catalog_end_exclusive || data.planning_end_exclusive;
    if (typeof catalogEnd === "string" && catalogEnd < windowEnd) {
      const notice = byId("window-notice");
      notice.hidden = false;
      notice.textContent = `Los datos exportados llegan hasta el ${readableDate(catalogEnd)} (excluido). Para completar la ventana actual ejecuta python src/eclipse_pipeline.py.`;
    }
    const ready = events.filter((event) => event.status === "ready").length;
    const partial = events.filter((event) => event.status === "partial").length;
    const pending = events.length - ready - partial;
    summary.append(node("span", "", `${ready} preparados · ${partial} parciales · ${pending} pendientes`));
    const generated = dateValue(data.generated_at_utc);
    if (generated) byId("prepared-at").textContent = `Datos exportados: ${dateTimeFormatter.format(generated)} UTC`;
    familySelect.addEventListener("change", fillEventOptions);
    eventSelect.addEventListener("change", renderEvent);
    countrySelect.addEventListener("change", () => chooseCountry(countrySelect.value));
    showMore.addEventListener("click", () => {
      expanded = !expanded;
      renderLocations();
    });
    byId("map-zoom-in").addEventListener("click", () => { mapZoom = Math.min(8, mapZoom * 1.6); updateMapView(); });
    byId("map-zoom-out").addEventListener("click", () => { mapZoom = Math.max(1, mapZoom / 1.6); updateMapView(); });
    byId("map-reset").addEventListener("click", () => {
      mapZoom = 1;
      mapCenter = [mapWidth / 2, mapHeight / 2];
      updateMapView();
    });
    fillEventOptions();
    if (!events.length) {
      const empty = byId("load-error");
      empty.hidden = false;
      empty.append(node("strong", "", "No hay eclipses en el catálogo local"), node("p", "", "Revisa la ventana de fechas y prepara de nuevo los datos."));
    }
  }

  initialize();
})();
