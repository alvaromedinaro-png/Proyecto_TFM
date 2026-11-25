"use strict";

(() => {
  const data = window.ECLIPSE_PLANNER_DATA;
  const world = window.ECLIPSE_WORLD_MAP;
  const byId = (id) => document.getElementById(id);
  const familySelect = byId("family-selector");
  const eventSelect = byId("event-selector");
  const countrySelect = byId("country-selector");
  const sortSelect = byId("sort-selector");
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
  let destinationKey = null; // zona cuyo panel de destino está abierto
  let mapSvg = null;
  let mapZoom = 1;
  let mapCenter = [mapWidth / 2, mapHeight / 2];
  const mapMarkers = new Map();
  // Zoom máximo y nivel a partir del cual se rotulan los puntos. A ×64 dos zonas a 15 km
  // (Tarifa y Algeciras) quedan a más de un centímetro en pantalla.
  const maxMapZoom = 64;
  const labelZoom = 10;
  // Mapa con teselas (Leaflet). Si Leaflet no carga, por ejemplo sin conexión, se dibuja el SVG local.
  let leafletMap = null;
  let tileGroup = null;
  let countryLayer = null;
  let markerLayer = null;
  const worldBounds = [[-56, -168], [74, 180]];
  const leafletMaxZoom = 16;
  const leafletLabelZoom = 6;
  // A partir de este nivel las fronteras de Natural Earth (1:110m) ya no coinciden con las
  // del mapa base, así que el sombreado de países se atenúa y manda el mapa de teselas.
  const leafletNearZoom = 5;
  const esriTiles = "https://server.arcgisonline.com/ArcGIS/rest/services";

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

  // ---------- Cuenta atrás del evento ----------

  // Mismo horizonte que el pronóstico en directo de live.js (Open-Meteo, 16 días).
  const forecastHorizonDays = 16;
  const dayMs = 86400000;

  // Primer máximo local del evento entre sus zonas; sin horas, el inicio del día en UTC.
  function eventReference(event) {
    const peaks = (Array.isArray(event.locations) ? event.locations : [])
      .map((location) => dateValue(location.peak_time_utc))
      .filter(Boolean)
      .map((date) => date.getTime());
    if (peaks.length) return new Date(Math.min(...peaks));
    return dateValue(event.event_date);
  }

  function countdownText(event, now = new Date()) {
    const reference = eventReference(event);
    if (!reference) return "";
    const days = (reference.getTime() - now.getTime()) / dayMs;
    if (days < -0.5) return "Este eclipse ya ha pasado.";
    if (days <= 0) return "El eclipse es hoy. Elige un destino para consultar el pronóstico real.";
    if (days < 1) return "Falta menos de 1 día. Elige un destino para consultar el pronóstico real.";
    const left = Math.ceil(days);
    const countdown = left === 1 ? "Falta 1 día" : `Faltan ${number.format(left)} días`;
    if (days <= forecastHorizonDays - 1) return `${countdown}. Ya hay pronóstico real: elige un destino para consultarlo.`;
    const from = new Date(reference.getTime() - (forecastHorizonDays - 1) * dayMs);
    return `${countdown}. Pronóstico real a partir del ${dateFormatter.format(from)}; hasta entonces, climatología histórica.`;
  }

  // ---------- Añadir al calendario (.ics) ----------

  // Texto de iCalendar (RFC 5545): se escapan barra, punto y coma, coma y saltos de línea.
  function icsText(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
  }

  function icsDate(date) {
    return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  }

  // Las líneas no pueden pasar de 75 octetos; las de continuación empiezan con un espacio.
  function icsFold(line) {
    const encoder = new TextEncoder();
    const parts = [];
    let current = "";
    let size = 0;
    for (const char of line) {
      const bytes = encoder.encode(char).length;
      if (size + bytes > (parts.length ? 74 : 75)) {
        parts.push(current);
        current = "";
        size = 0;
      }
      current += char;
      size += bytes;
    }
    parts.push(current);
    return parts.join("\r\n ");
  }

  function slug(value) {
    return String(value || "zona").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "zona";
  }

  // Evento de calendario centrado en el máximo del lugar. Dura la fase calculada (mínimo
  // 30 minutos) y avisa un día antes; las horas van en UTC y el calendario las pasa a la local.
  function calendarFile(location, event) {
    const peak = dateValue(location.peak_time_utc);
    if (!peak) return null;
    const lasting = finite(location.phase_duration_s) ? Math.max(location.phase_duration_s * 1000, 30 * 60000) : 60 * 60000;
    const start = new Date(peak.getTime() - lasting / 2);
    const end = new Date(peak.getTime() + lasting / 2);
    const lunar = event.event_family === "lunar";
    const country = locationCountry(location);
    const name = location.name || "";
    // Algunos nombres de NASA ya incluyen el país («Ndjamena, CHAD»).
    const place = country && !name.toLocaleLowerCase("es").includes(country.toLocaleLowerCase("es")) ? [name, country].filter(Boolean).join(", ") : name || country;
    const formatter = typeof location.timezone === "string" && location.timezone ? localFormatter(location.timezone) : null;
    const peakLine = formatter
      ? `${lunar ? "Máximo global" : "Máximo local"}: ${formatter.time.format(peak)} (${formatter.day.format(peak)}), ${utcTimeFormatter.format(peak)} UTC.`
      : `${lunar ? "Máximo global" : "Máximo local"}: ${peakTime(location.peak_time_utc)}.`;
    const phase = phaseNames[location.local_phase] || location.local_phase;
    const description = [
      peakLine,
      phase ? `Fase visible: ${phase}.` : "",
      event.event_family === "solar" && finite(location.obscuration_pct) ? `Disco solar oculto: ${formatted(location.obscuration_pct, " %")}.` : "",
      finite(location.clear_probability_pct) ? `Cielo despejado en el ${formatted(location.clear_probability_pct, " %")} de los años a esa hora (lo habitual, no un pronóstico).` : "",
      "El inicio y el fin del evento son aproximados; la referencia es el máximo.",
      lunar ? "" : "Nunca mires al Sol sin gafas de eclipse homologadas (ISO 12312-2).",
      "Planificador de eclipses · datos NASA GSFC y Open-Meteo."
    ].filter(Boolean).join("\n");
    const lines = [
      "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Planificador de eclipses//ES", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
      "BEGIN:VEVENT",
      `UID:${slug(event.event_id)}-${slug(location.name)}-${icsDate(peak)}@planificador-eclipses`,
      `DTSTAMP:${icsDate(new Date())}`,
      `DTSTART:${icsDate(start)}`,
      `DTEND:${icsDate(end)}`,
      `SUMMARY:${icsText(`${eventTitle(event)} · ${location.name || "zona"}`)}`,
      `LOCATION:${icsText(place)}`,
      finite(location.latitude) && finite(location.longitude) ? `GEO:${location.latitude.toFixed(5)};${location.longitude.toFixed(5)}` : "",
      `DESCRIPTION:${icsText(description)}`,
      "BEGIN:VALARM", "ACTION:DISPLAY", `DESCRIPTION:${icsText(`Mañana: ${eventTitle(event).toLocaleLowerCase("es")} en ${location.name || "tu destino"}`)}`, "TRIGGER:-P1D", "END:VALARM",
      "END:VEVENT", "END:VCALENDAR"
    ].filter(Boolean);
    return {
      name: `eclipse-${event.event_date || icsDate(peak).slice(0, 8)}-${slug(location.name)}.ics`,
      content: lines.map(icsFold).join("\r\n") + "\r\n"
    };
  }

  function calendarButton(location, event) {
    const button = node("button", "secondary-button calendar-button", "Añadir al calendario");
    button.type = "button";
    const file = calendarFile(location, event);
    button.disabled = !file;
    button.title = "Descarga un archivo .ics para Google Calendar, Outlook o Apple Calendar";
    button.addEventListener("click", () => {
      const current = calendarFile(location, event);
      if (!current) return;
      const url = URL.createObjectURL(new Blob([current.content], { type: "text/calendar;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = current.name;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    return button;
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

  const utcTimeFormatter = new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  const localFormatters = new Map();

  function localFormatter(timeZone) {
    if (!localFormatters.has(timeZone)) {
      try {
        localFormatters.set(timeZone, {
          time: new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit", timeZone, timeZoneName: "short" }),
          day: new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", timeZone })
        });
      } catch (error) {
        localFormatters.set(timeZone, null);
      }
    }
    return localFormatters.get(timeZone);
  }

  // Hora local del lugar (zona IANA exportada; el navegador aplica el horario de verano) y UTC.
  function peakStat(location, event) {
    const lunar = event.event_family === "lunar";
    const label = lunar ? "Máximo global" : "Máximo local";
    const fallbackNote = lunar ? "Hora del máximo global lunar" : "Hora aproximada";
    const date = dateValue(location.peak_time_utc);
    const formatter = date && typeof location.timezone === "string" && location.timezone ? localFormatter(location.timezone) : null;
    if (!formatter) return stat(label, peakTime(location.peak_time_utc), fallbackNote);
    // La fecha local puede no coincidir con la del eclipse en UTC, así que se indica siempre.
    return stat(label, formatter.time.format(date), `${formatter.day.format(date)} · ${utcTimeFormatter.format(date)} UTC · hora local${lunar ? " del máximo global" : " aproximada"}`);
  }

  const bandLabels = { baja: "Baja", media: "Media", alta: "Alta" };

  // Probabilidad con su categoría (experimento de ML) y su rango al 95 % (punto = valor, tramo = intervalo).
  function probabilityStat(label, value, range, band, years, kind, className = "") {
    const item = stat(label, formatted(value, " %"), sampleLabel(years), className);
    const content = item.querySelector("dd");
    const note = content.querySelector("small");
    if (finite(value) && bandLabels[band]) {
      const chip = node("span", `band band-${kind}-${band}`, bandLabels[band]);
      chip.title = kind === "clear" ? "Categoría de cielo despejado: baja < 25 %, media 25-50 %, alta ≥ 50 %" : "Categoría de lluvia: baja < 25 %, media 25-50 %, alta ≥ 50 %";
      content.insertBefore(chip, note);
    }
    if (finite(value) && Array.isArray(range) && range.length === 2 && range.every(finite) && note) {
      note.textContent = `Rango 95 %: ${number.format(range[0])}-${number.format(range[1])} % · ${sampleLabel(years)}`;
      const bar = node("span", `range-bar range-${kind}`);
      bar.setAttribute("aria-hidden", "true");
      const span = node("span", "range-span");
      span.style.left = `${Math.max(0, Math.min(100, range[0]))}%`;
      span.style.width = `${Math.max(0, Math.min(100, range[1]) - Math.max(0, range[0]))}%`;
      const dot = node("span", "range-dot");
      dot.style.left = `${Math.max(0, Math.min(100, value))}%`;
      bar.append(span, dot);
      content.insertBefore(bar, note);
    }
    return item;
  }

  // Los porcentajes son climatología: se dice junto a la cifra para que no se lean como pronóstico.
  function climateMeaning(location) {
    const years = finite(location.valid_cloud_years) && location.valid_cloud_years > 0 ? `los últimos ${number.format(location.valid_cloud_years)} años` : "los años disponibles";
    return node("p", "climate-meaning", `Lo habitual en este lugar y época: en cuántos de ${years} estuvo despejado o llovió a esa hora. No es el pronóstico del día del eclipse.`);
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

  // Enlace a la explicación del tipo de eclipse en la guía (eclipses.html).
  const guideAnchors = {
    solar: { total: "solar-total", annular: "solar-anular", hybrid: "solar-hibrido", partial: "solar-parcial" },
    lunar: { total: "lunar-total", partial: "lunar-parcial", penumbral: "lunar-penumbral" }
  };

  function updateGuideLink(event) {
    const link = byId("event-guide");
    const family = event.event_family === "lunar" ? "lunar" : "solar";
    const anchor = guideAnchors[family][event.eclipse_type];
    link.href = anchor ? `eclipses.html#${anchor}` : "eclipses.html";
    link.textContent = anchor ? `¿Qué es un ${eventTitle(event).toLocaleLowerCase("es")}?` : "Tipos de eclipse";
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

  // Orden de las tarjetas. «Recomendado» es el ranking del Gold (validado en el experimento de ML):
  // fase, cobertura climática suficiente y puntuación; las zonas sin climatología van al final de su fase.
  const metric = (value) => finite(value) ? value : -Infinity;
  const sorters = {
    recommended: (a, b) => metric(b.phase_priority) - metric(a.phase_priority)
      || Number(finite(b.recommendation_score)) - Number(finite(a.recommendation_score))
      || Number(!b.ranking_provisional) - Number(!a.ranking_provisional)
      || metric(b.recommendation_score) - metric(a.recommendation_score),
    // A igual cielo despejado, primero la que menos llueve.
    clear: (a, b) => metric(b.clear_probability_pct) - metric(a.clear_probability_pct)
      || metric(finite(b.rain_probability_pct) ? -b.rain_probability_pct : null) - metric(finite(a.rain_probability_pct) ? -a.rain_probability_pct : null),
    astronomy: () => 0,
    duration: (a, b) => metric(b.phase_priority) - metric(a.phase_priority) || metric(b.phase_duration_s) - metric(a.phase_duration_s)
  };
  const sortNotes = {
    recommended: "Orden recomendado: primero totalidad o anularidad; dentro de cada fase, cielo despejado habitual, lluvia, ocultación, duración y altura del Sol.",
    clear: "Ordenadas por cielo despejado habitual.",
    duration: "Ordenadas por fase y, dentro de cada fase, por duración."
  };

  function sortMode() {
    return sorters[sortSelect.value] ? sortSelect.value : "recommended";
  }

  function sortLocations() {
    const sorter = sorters[sortMode()];
    locations.sort((a, b) => sorter(a, b) || locationSort(a, b)); // NaN (ambos sin dato) cuenta como empate
  }

  function updateSortOptions() {
    const option = sortSelect.querySelector('option[value="astronomy"]');
    option.textContent = selectedEvent && selectedEvent.event_family === "lunar" ? "Altura de la Luna" : "Disco solar oculto";
  }

  function prepareCountries() {
    locations = (Array.isArray(selectedEvent.locations) ? selectedEvent.locations : []).map((location, index) => ({
      ...location, _key: `${selectedEvent.event_id}-${index}`, _country: countryCode(location.country_code)
    })).filter(eligibleLocation);
    updateSortOptions();
    sortLocations();
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
    astronomy.append(peakStat(location, event));
    astronomy.append(stat("Altura sobre el horizonte", formatted(location.body_altitude_deg, "°"), event.event_family === "lunar" ? "Luna en el máximo global" : "Sol en el máximo local"));
    card.append(astronomy);
    if (!hasClimate(location)) {
      const pending = node("p", "climate-pending");
      pending.append(node("strong", "", "Climatología pendiente"), document.createTextNode("La geometría del eclipse está calculada para esta zona; sus datos meteorológicos aún no están preparados."));
      card.append(pending, calendarButton(location, event));
      return card;
    }
    card.append(node("h5", "climate-heading", "Lo habitual a la hora del eclipse"));
    const climate = node("dl", "climate-stats");
    climate.append(probabilityStat("Años con cielo despejado", location.clear_probability_pct, location.clear_range_pct, location.clear_band, location.valid_cloud_years, "clear"));
    climate.append(probabilityStat("Años con lluvia", location.rain_probability_pct, location.rain_range_pct, location.rain_band, location.valid_rain_years, "rain", "rain-stat"));
    card.append(climate, climateMeaning(location));
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
    card.append(destinationButton(location), calendarButton(location, event));
    return card;
  }

  // Elegir el destino abre el panel con el pronóstico y los alojamientos consultados en directo.
  function destinationButton(location) {
    const chosen = destinationKey === location._key;
    const button = node("button", `destination-button${chosen ? " chosen" : ""}`, chosen ? "Destino elegido · ver panel" : "Elegir como destino");
    button.type = "button";
    button.disabled = !finite(location.latitude) || !finite(location.longitude) || !window.EclipseLive;
    button.addEventListener("click", () => chooseLocation(location, true));
    return button;
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
    const lunar = selectedEvent.event_family === "lunar";
    const order = sortMode() === "astronomy" ? (lunar ? "Ordenadas por altura de la Luna." : "Ordenadas por disco solar oculto.")
      : sortMode() === "recommended" && lunar ? "Orden recomendado: cielo despejado habitual, lluvia y altura de la Luna."
      : sortNotes[sortMode()];
    const cdsCount = filtered.filter((location) => location.climate_source === "copernicus_cds_era5").length;
    const sourceNote = cdsCount ? ` Climatología ERA5; en ${cdsCount} ${cdsCount === 1 ? "zona" : "zonas"} se obtuvo de Copernicus por regiones.` : "";
    byId("locations-note").textContent = filtered.length ? `${filtered.length} zonas analizadas · ${preparedCount} con climatología. ${order}${sourceNote}` : "Los resultados aparecerán cuando estén preparados.";
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
    if (leafletMap) {
      const zoom = leafletMap.getZoom();
      const container = byId("map-container");
      container.classList.toggle("show-labels", zoom >= leafletLabelZoom);
      container.classList.toggle("map-near", zoom >= leafletNearZoom);
      byId("map-zoom-in").disabled = zoom >= leafletMap.getMaxZoom();
      byId("map-zoom-out").disabled = zoom <= leafletMap.getMinZoom();
      return;
    }
    if (!mapSvg) return;
    const width = mapWidth / mapZoom;
    const height = mapHeight / mapZoom;
    mapCenter[0] = Math.max(width / 2, Math.min(mapWidth - width / 2, mapCenter[0]));
    mapCenter[1] = Math.max(height / 2, Math.min(mapHeight - height / 2, mapCenter[1]));
    mapSvg.setAttribute("viewBox", `${mapCenter[0] - width / 2} ${mapCenter[1] - height / 2} ${width} ${height}`);
    mapMarkers.forEach((entry) => {
      entry.dot.setAttribute("r", 4.4 / mapZoom);
      entry.hit.setAttribute("r", 9 / mapZoom);
      entry.focus.setAttribute("r", 7.5 / mapZoom);
      const strokeWidth = entry.group.classList.contains("selected") ? 2 : entry.group.classList.contains("pending") ? 1.6 : 1.2;
      entry.dot.setAttribute("stroke-width", strokeWidth / mapZoom);
      entry.focus.setAttribute("stroke-width", 1.7 / mapZoom);
      entry.label.setAttribute("x", 7 / mapZoom);
      entry.label.setAttribute("y", 3.6 / mapZoom);
      entry.label.setAttribute("font-size", 10.5 / mapZoom);
      entry.label.setAttribute("stroke-width", 3 / mapZoom);
    });
    // Con el mapa ampliado se rotulan los puntos para distinguir zonas cercanas.
    mapSvg.classList.toggle("show-labels", mapZoom >= labelZoom);
    byId("map-container").classList.toggle("can-drag", mapZoom > 1);
    byId("map-zoom-in").disabled = mapZoom >= maxMapZoom;
    byId("map-zoom-out").disabled = mapZoom <= 1;
  }

  // Acerca o aleja manteniendo fijo el punto del mapa que está bajo el cursor.
  function zoomMapAt(factor, svgPoint) {
    const next = Math.max(1, Math.min(maxMapZoom, mapZoom * factor));
    if (next === mapZoom) return;
    const anchor = svgPoint || mapCenter;
    mapCenter = [anchor[0] - (anchor[0] - mapCenter[0]) * mapZoom / next, anchor[1] - (anchor[1] - mapCenter[1]) * mapZoom / next];
    mapZoom = next;
    updateMapView();
  }

  function svgPointFromEvent(event) {
    const matrix = mapSvg && mapSvg.getScreenCTM();
    if (!matrix) return null;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    return [point.x, point.y];
  }

  function focusCountry(code) {
    if (leafletMap) {
      const points = locations.filter((location) => location._country === code && finite(location.latitude) && finite(location.longitude)).map((location) => [location.latitude, location.longitude]);
      if (!points.length || code === "all") resetLeafletView();
      else leafletMap.fitBounds(points, { padding: [50, 50], maxZoom: 7 });
      return;
    }
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
    if (leafletMap) {
      countryLayer.eachLayer((layer) => {
        const element = layer.getElement && layer.getElement();
        if (!element) return;
        const selected = layer.feature.properties._code === countrySelect.value;
        element.classList.toggle("selected", selected);
        if (element.classList.contains("available")) element.setAttribute("aria-pressed", String(selected));
      });
      mapMarkers.forEach(({ marker }, key) => {
        const location = locations.find((item) => item._key === key);
        const visible = countrySelect.value === "all" || location._country === countrySelect.value;
        if (visible && !markerLayer.hasLayer(marker)) markerLayer.addLayer(marker);
        if (!visible && markerLayer.hasLayer(marker)) markerLayer.removeLayer(marker);
        const selected = selectedLocation === key;
        marker.setZIndexOffset(selected ? 1000 : 0);
        const element = marker.getElement();
        if (element) {
          element.classList.toggle("selected", selected);
          element.setAttribute("aria-pressed", String(selected));
        }
      });
    } else if (mapSvg) {
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
    destinationKey = null;
    if (window.EclipseLive) window.EclipseLive.hide();
    renderLocations();
    updateMapSelection();
    showMapHint();
    if (zoom) focusCountry(code);
  }

  // Selecciona una zona sin mover la página: se resalta en el mapa y en su tarjeta, y sus datos
  // aparecen en la ficha junto al mapa. Solo «Elegir como destino» abre el panel de abajo.
  function chooseLocation(location, openPanel = false) {
    // El filtro de país solo cambia si ocultaría la zona elegida.
    if (countrySelect.value !== "all" && countrySelect.value !== location._country) countrySelect.value = location._country;
    selectedLocation = location._key;
    if (!openPanel && destinationKey !== location._key) {
      destinationKey = null;
      if (window.EclipseLive) window.EclipseLive.hide();
    }
    if (openPanel) destinationKey = location._key;
    const index = visibleLocations().findIndex((item) => item._key === selectedLocation);
    expanded = expanded || index >= 3;
    renderLocations();
    updateMapSelection();
    showMapDetail(location);
    if (!openPanel) return;
    if (window.EclipseLive && finite(location.latitude) && finite(location.longitude)) window.EclipseLive.show(location, selectedEvent);
    const panel = byId("destination-panel");
    if (panel && !panel.hidden) {
      panel.focus({ preventScroll: true });
      panel.scrollIntoView({ block: "start", behavior: "auto" });
    }
  }

  function scrollToCard(location) {
    const card = byId(`location-${location._key}`);
    if (!card) return;
    card.focus({ preventScroll: true });
    card.scrollIntoView({ block: "center", behavior: "auto" });
  }

  function showMapHint() {
    const detail = byId("map-detail");
    if (!detail) return;
    const box = node("div", "map-detail-hint");
    box.append(node("strong", "", "Elige un punto del mapa"), node("p", "", "Sus datos aparecerán aquí sin salir del mapa. Usa la rueda del ratón o los botones + y − para acercar; al ampliar se muestran los nombres de las zonas."));
    detail.replaceChildren(box);
  }

  function metricText(location) {
    return selectedEvent.event_family === "solar" ? `${formatted(location.obscuration_pct, " %")} oculto` : `Luna a ${formatted(location.body_altitude_deg, "°")}`;
  }

  // Ficha compacta junto al mapa con los datos de la zona seleccionada.
  function showMapDetail(location) {
    const detail = byId("map-detail");
    if (!detail) return;
    const head = node("div", "map-detail-head");
    head.append(node("h4", "", location.name || "Zona sin nombre"), node("span", "location-country", locationCountry(location)));
    const localPhase = phaseNames[location.local_phase] || location.local_phase;
    if (localPhase) head.append(node("span", "phase-label", `Fase visible: ${localPhase}`));
    const astronomy = node("dl", "map-detail-stats");
    if (selectedEvent.event_family === "solar") astronomy.append(stat("Disco solar oculto", formatted(location.obscuration_pct, " %")));
    else astronomy.append(stat("Altura de la Luna", formatted(location.body_altitude_deg, "°")));
    const central = location.duration_scope === "local_central";
    astronomy.append(stat(central ? "Fase central" : location.duration_scope === "global_lunar_phase" ? "Fase global" : "Duración visible", duration(location.phase_duration_s)));
    astronomy.append(peakStat(location, selectedEvent));
    if (selectedEvent.event_family === "solar") astronomy.append(stat("Altura del Sol", formatted(location.body_altitude_deg, "°")));
    detail.replaceChildren(head, astronomy);
    if (hasClimate(location)) {
      const climate = node("dl", "map-detail-stats climate");
      climate.append(probabilityStat("Años despejados", location.clear_probability_pct, location.clear_range_pct, location.clear_band, location.valid_cloud_years, "clear"));
      climate.append(probabilityStat("Años con lluvia", location.rain_probability_pct, location.rain_range_pct, location.rain_band, location.valid_rain_years, "rain", "rain-stat"));
      detail.append(climate, climateMeaning(location));
    } else {
      detail.append(node("p", "climate-pending", "Climatología pendiente para esta zona."));
    }
    const actions = node("div", "map-detail-actions");
    const choose = destinationButton(location);
    const card = node("button", "secondary-button", "Ver la tarjeta completa");
    card.type = "button";
    card.addEventListener("click", () => scrollToCard(location));
    actions.append(choose, calendarButton(location, selectedEvent), card);
    detail.append(actions);
  }

  // Al pulsar un punto se buscan los que se solapan en pantalla. Si hay varios, la ficha los lista
  // y el mapa se acerca a ellos para separarlos.
  function pickFromMap(location) {
    let screen;
    if (leafletMap) {
      // Con una animación de zoom en curso las posiciones en pantalla no son las finales.
      leafletMap.stop();
      // Posición en píxeles del contenedor de Leaflet al zoom actual.
      screen = (item) => {
        const point = leafletMap.latLngToContainerPoint([item.latitude, item.longitude]);
        return [point.x, point.y];
      };
    } else {
      const matrix = mapSvg && mapSvg.getScreenCTM();
      // Píxeles de pantalla por unidad del SVG; ya incluye el zoom del viewBox.
      const pixelsPerUnit = matrix ? Math.hypot(matrix.a, matrix.b) : 1;
      screen = (item) => project(item.longitude, item.latitude).map((value) => value * pixelsPerUnit);
    }
    const isShown = (entry) => leafletMap ? markerLayer.hasLayer(entry.marker) : !entry.group.hidden;
    const [x, y] = screen(location);
    const near = locations.filter((item) => {
      const entry = mapMarkers.get(item._key);
      if (!entry || !isShown(entry) || !finite(item.latitude) || !finite(item.longitude)) return false;
      const [ix, iy] = screen(item);
      return Math.hypot(ix - x, iy - y) < 12;
    });
    if (near.length <= 1) {
      chooseLocation(location);
      return;
    }
    if (leafletMap) {
      const zoom = Math.min(leafletMaxZoom, Math.max(leafletMap.getZoom() + 2, leafletLabelZoom));
      leafletMap.fitBounds(near.map((item) => [item.latitude, item.longitude]), { padding: [70, 70], maxZoom: zoom });
    } else {
      const cx = near.reduce((sum, item) => sum + project(item.longitude, item.latitude)[0], 0) / near.length;
      const cy = near.reduce((sum, item) => sum + project(item.longitude, item.latitude)[1], 0) / near.length;
      zoomMapAt(4, [cx, cy]);
    }
    const detail = byId("map-detail");
    const box = node("div", "map-detail-cluster");
    box.append(node("strong", "", `${near.length} zonas muy próximas`), node("p", "", "El mapa se ha acercado para separarlas. Elige una:"));
    const list = node("ul", "cluster-list");
    // Primero la zona pulsada y después las demás por cercanía.
    near.sort((a, b) => Math.hypot(project(a.longitude, a.latitude)[0] - x, project(a.longitude, a.latitude)[1] - y)
      - Math.hypot(project(b.longitude, b.latitude)[0] - x, project(b.longitude, b.latitude)[1] - y)).forEach((item) => {
      const entry = node("li");
      const button = node("button", "cluster-item");
      button.type = "button";
      button.append(node("strong", "", item.name || "Zona"), node("span", "", `${locationCountry(item)} · ${metricText(item)}${hasClimate(item) && finite(item.clear_probability_pct) ? ` · despejado el ${formatted(item.clear_probability_pct, " %")} de los años` : ""}`));
      button.addEventListener("click", () => chooseLocation(item));
      entry.append(button);
      list.append(entry);
    });
    box.append(list);
    detail.replaceChildren(box);
  }

  // Arrastrar con el botón izquierdo (o el dedo) desplaza el mapa ampliado.
  // Si el puntero se ha movido, se anula el clic siguiente para no seleccionar
  // el país o el punto donde termina el arrastre.
  function enableMapDrag(container) {
    let drag = null;
    let suppressClick = false;
    container.addEventListener("pointerdown", (event) => {
      if (!mapSvg || event.button !== 0 || mapZoom <= 1) return;
      const matrix = mapSvg.getScreenCTM();
      if (!matrix || !matrix.a) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, center: [...mapCenter], scale: 1 / matrix.a, moved: false };
    });
    container.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 4) return;
        drag.moved = true;
        container.setPointerCapture(drag.id);
        container.classList.add("dragging");
      }
      event.preventDefault();
      mapCenter = [drag.center[0] - dx * drag.scale, drag.center[1] - dy * drag.scale];
      updateMapView();
    });
    const endDrag = (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      suppressClick = drag.moved;
      drag = null;
      container.classList.remove("dragging");
    };
    container.addEventListener("pointerup", endDrag);
    container.addEventListener("pointercancel", endDrag);
    container.addEventListener("click", (event) => {
      if (!suppressClick) return;
      suppressClick = false;
      event.stopPropagation();
      event.preventDefault();
    }, true);
  }

  function darkTheme() {
    const root = document.documentElement;
    return root.dataset.theme === "dark" || (!root.dataset.theme && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  // Teselas de Esri, las mismas que el mapa del destino: no piden clave ni cabecera Referer y
  // funcionan con la página abierta como archivo local. Si ninguna carga, se rellenan los países
  // con el color de tierra para que el mapa siga siendo legible.
  function setWorldTiles() {
    const container = byId("map-container");
    tileGroup.clearLayers();
    let loaded = false;
    const urls = darkTheme()
      ? [`${esriTiles}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, `${esriTiles}/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`]
      : [`${esriTiles}/World_Street_Map/MapServer/tile/{z}/{y}/{x}`];
    urls.forEach((url, position) => {
      const layer = window.L.tileLayer(url, {
        maxZoom: leafletMaxZoom,
        attribution: position ? "" : 'Teselas &copy; <a href="https://www.esri.com/">Esri</a> · Países &copy; <a href="https://www.naturalearthdata.com/">Natural Earth</a>'
      });
      layer.on("tileload", () => { loaded = true; container.classList.remove("tiles-offline"); });
      layer.on("tileerror", () => { if (!loaded) container.classList.add("tiles-offline"); });
      tileGroup.addLayer(layer);
    });
  }

  function resetLeafletView() {
    if (leafletMap) leafletMap.fitBounds(worldBounds, { padding: [4, 4] });
  }

  function ensureLeafletMap(container) {
    if (leafletMap) return leafletMap;
    container.replaceChildren();
    container.classList.add("leaflet-world");
    leafletMap = window.L.map(container, {
      zoomControl: false,
      zoomSnap: 0.25,
      zoomDelta: 1,
      wheelPxPerZoomLevel: 90,
      minZoom: 1,
      maxZoom: leafletMaxZoom,
      maxBounds: [[-85, -200], [85, 200]],
      maxBoundsViscosity: 1,
      worldCopyJump: false
    });
    tileGroup = window.L.layerGroup().addTo(leafletMap);
    setWorldTiles();
    countryLayer = window.L.layerGroup().addTo(leafletMap);
    markerLayer = window.L.layerGroup().addTo(leafletMap);
    leafletMap.on("zoomend", updateMapView);
    // El mapa sigue el modo día / noche de la página.
    new MutationObserver(setWorldTiles).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", setWorldTiles);
    // El tamaño del contenedor cambia con el diseño adaptable; Leaflet tiene que saberlo.
    if (window.ResizeObserver) new ResizeObserver(() => leafletMap.invalidateSize()).observe(container);
    return leafletMap;
  }

  // Leaflet no hace enfocables sus polígonos: se añade el acceso por teclado a mano.
  function makeAccessible(element, label, action) {
    if (!element || element.dataset.keyboard) return;
    element.dataset.keyboard = "1";
    element.setAttribute("role", "button");
    element.setAttribute("tabindex", "0");
    element.setAttribute("aria-label", label);
    element.setAttribute("aria-pressed", "false");
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        action();
      }
    });
  }

  function renderLeafletMap(container) {
    const map = ensureLeafletMap(container);
    countryLayer.clearLayers();
    markerLayer.clearLayers();
    mapMarkers.clear();
    world.features.forEach((feature) => {
      if (!feature || !feature.geometry) return;
      const properties = feature.properties || {};
      const code = countryCode(properties.country_code);
      const country = countries.find((item) => item.country_code === code);
      const title = country ? `${country.country_name}${selectedEvent.event_family === "solar" ? `: hasta ${formatted(country.max_obscuration_pct, " %")} de disco solar oculto en las zonas analizadas` : ": zonas visibles en el máximo global lunar"}` : properties.country_name || "Territorio";
      const layer = window.L.geoJSON({ ...feature, properties: { ...properties, _code: code } }, {
        className: `map-country${country ? " available" : ""}`,
        interactive: Boolean(country),
        // Los colores los pone la hoja de estilos según el tema y el zoom.
        style: { weight: 1, fillOpacity: 1 }
      });
      layer.eachLayer((path) => {
        if (!country) return;
        path.bindTooltip(title, { sticky: true, className: "map-tooltip", direction: "top", offset: [0, -6] });
        path.on("click", () => chooseCountry(code));
        path.on("add", () => makeAccessible(path.getElement(), `Seleccionar ${title}`, () => chooseCountry(code)));
        countryLayer.addLayer(path);
      });
      if (!country) layer.eachLayer((path) => countryLayer.addLayer(path));
    });
    locations.forEach((location) => {
      if (!finite(location.latitude) || !finite(location.longitude)) return;
      const label = `${location.name || "Zona"}, ${locationCountry(location)}${selectedEvent.event_family === "solar" ? `: ${formatted(location.obscuration_pct, " %")} de disco solar oculto` : `: Luna a ${formatted(location.body_altitude_deg, "°")}`}${hasClimate(location) ? "" : ". Climatología pendiente"}`;
      const icon = window.L.divIcon({ className: `map-pin${hasClimate(location) ? "" : " pending"}`, html: "<span></span>", iconSize: [16, 16], iconAnchor: [8, 8] });
      // keyboard: true hace el punto enfocable y Enter equivale a pulsarlo.
      const marker = window.L.marker([location.latitude, location.longitude], { icon, keyboard: true, title: label, riseOnHover: true });
      marker.bindTooltip(location.name || "", { permanent: true, direction: "right", offset: [8, 0], className: "map-pin-label", interactive: false });
      marker.on("click", () => pickFromMap(location));
      marker.on("add", () => {
        const element = marker.getElement();
        if (!element) return;
        element.setAttribute("role", "button");
        element.setAttribute("aria-label", `Ver ${label}`);
      });
      mapMarkers.set(location._key, { marker });
      markerLayer.addLayer(marker);
    });
    ["map-zoom-in", "map-zoom-out", "map-reset"].forEach((id) => { byId(id).disabled = false; });
    resetLeafletView();
    // El contenedor puede haber estado oculto hasta ahora: Leaflet recalcula su tamaño.
    setTimeout(() => { map.invalidateSize(); resetLeafletView(); }, 0);
    updateMapSelection();
    showMapHint();
  }

  function renderMap() {
    const container = byId("map-container");
    const mapAvailableWithTiles = window.L && world && world.type === "FeatureCollection" && Array.isArray(world.features) && world.features.length;
    if (mapAvailableWithTiles) {
      renderLeafletMap(container);
      return;
    }
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
      const name = svgNode("text", { class: "marker-label", "aria-hidden": "true" });
      name.textContent = location.name || "";
      group.append(title, hit, focus, dot, name);
      activateOnKeyboard(group, () => pickFromMap(location));
      mapMarkers.set(location._key, { group, hit, focus, dot, label: name });
      markers.append(group);
    });
    mapSvg.append(markers);
    container.append(mapSvg);
    updateMapSelection();
    updateMapView();
    showMapHint();
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
    destinationKey = null;
    if (window.EclipseLive) window.EclipseLive.hide();
    byId("event-date").textContent = readableDate(selectedEvent.event_date);
    byId("event-title").textContent = eventTitle(selectedEvent);
    byId("event-countdown").textContent = countdownText(selectedEvent);
    updateGuideLink(selectedEvent);
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
    else forecastNote.textContent = "Para estas fechas las tarjetas usan la climatología histórica. Al elegir un destino, el pronóstico real se consulta en directo cuando falten 16 días o menos para el eclipse.";
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
    sortSelect.addEventListener("change", () => {
      sortLocations();
      expanded = false;
      renderLocations();
    });
    showMore.addEventListener("click", () => {
      expanded = !expanded;
      renderLocations();
    });
    byId("destination-close").addEventListener("click", () => {
      const key = destinationKey;
      destinationKey = null;
      if (window.EclipseLive) window.EclipseLive.hide();
      renderLocations();
      const card = key && byId(`location-${key}`);
      if (card) card.focus();
    });
    enableMapDrag(byId("map-container"));
    byId("map-zoom-in").addEventListener("click", () => leafletMap ? leafletMap.zoomIn() : zoomMapAt(2));
    byId("map-zoom-out").addEventListener("click", () => leafletMap ? leafletMap.zoomOut() : zoomMapAt(1 / 2));
    // Rueda del ratón (o gesto de dos dedos en el panel táctil) sobre el mapa: zoom hacia el cursor.
    byId("map-container").addEventListener("wheel", (event) => {
      if (!mapSvg) return;
      // Con el cursor sobre el mapa la rueda solo hace zoom: la página no se desplaza
      // aunque el mapa ya esté al máximo o al mínimo.
      event.preventDefault();
      const steps = Math.max(-3, Math.min(3, -event.deltaY / (event.deltaMode === 1 ? 3 : 100)));
      zoomMapAt(Math.pow(1.35, steps), svgPointFromEvent(event));
    }, { passive: false });
    byId("map-reset").addEventListener("click", () => {
      if (leafletMap) {
        resetLeafletView();
        return;
      }
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
