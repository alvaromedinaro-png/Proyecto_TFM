"use strict";

// Consultas en directo para el destino elegido. El catálogo, la astronomía y la climatología
// llegan preparados en planner_data.js; aquí solo se consulta lo que cambia con el tiempo:
// - el pronóstico meteorológico de Open-Meteo, cuando el eclipse entra en su horizonte de 16 días;
// - los alojamientos registrados en OpenStreetMap (API Overpass) alrededor del destino;
// - con el servidor local, precios para las fechas y personas elegidas y fotos de cada alojamiento
//   (la de TripAdvisor y las de Wikimedia Commons tomadas a su alrededor).
// Solo se envían las coordenadas del destino, nunca la ubicación del usuario.
window.EclipseLive = (() => {
  const forecastDays = 16;
  const forecastUrl = "https://api.open-meteo.com/v1/forecast";
  // Instancias públicas de Overpass: se consultan a la vez y se usa la primera que responde,
  // porque cualquiera de ellas puede estar saturada. Si fallan todas, se recurre a Photon.
  const overpassUrls = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
  ];
  const photonUrl = "https://photon.komoot.io/api/";
  // Radio de búsqueda: 8 km y, si apenas hay resultados (zonas rurales), 25 km.
  const lodgingRadiusM = 8000;
  const wideLodgingRadiusM = 25000;
  const minLodgings = 5;
  const listedLodgings = 15;
  const clearCloudCoverPct = 20; // mismo umbral que «cielo despejado» en la climatología
  const lodgingTypes = {
    hotel: "Hotel", motel: "Motel", hostel: "Albergue", guest_house: "Casa de huéspedes",
    apartment: "Apartamento", chalet: "Casa rural", camp_site: "Camping"
  };
  const number = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 });
  const longDate = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  const lodgingCache = new Map();
  const byId = (id) => document.getElementById(id);
  let request = 0;
  let lodgingRequest = 0; // las fechas pueden cambiar sin volver a pedir el pronóstico
  let current = null;     // destino abierto en el panel: { location, day, stay }
  const commonsUrl = "https://commons.wikimedia.org/w/api.php";
  const commonsRadiusM = 150;
  const photoCache = new Map();
  const maxNights = 30;
  const maxAdults = 8;
  const maxRooms = 4;
  let map = null;
  let tileLayer = null;
  let markerLayer = null;
  let markersById = new Map();
  let siteMarker = null;
  let priceLayer = null;
  let serverStatus = null;
  const euro = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  }

  function link(text, href, className = "") {
    const anchor = node("a", className, text);
    anchor.href = href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  // Fecha local del máximo en la zona horaria del lugar (YYYY-MM-DD), o la fecha UTC si no la hay.
  function localDay(date, timeZone) {
    try {
      return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: timeZone || "UTC" }).format(date);
    } catch (error) {
      return date.toISOString().slice(0, 10);
    }
  }

  function shiftDay(day, offset) {
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + offset);
    return date.toISOString().slice(0, 10);
  }

  function distanceKm(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const a = Math.sin((lat2 - lat1) * rad / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin((lon2 - lon1) * rad / 2) ** 2;
    return 12742 * Math.asin(Math.sqrt(a));
  }

  async function fetchJson(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    if (options.signal) options.signal.addEventListener("abort", () => controller.abort());
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function message(container, title, text, className = "live-message") {
    const box = node("div", className);
    box.append(node("strong", "", title));
    if (text) box.append(node("p", "", text));
    container.replaceChildren(box);
  }

  // ---------- Pronóstico ----------

  function forecastStat(label, value, note) {
    const item = node("div", "stat");
    item.append(node("dt", "", label));
    const content = node("dd", value === "—" ? "unavailable" : "", value);
    if (note) content.append(node("small", "", note));
    item.append(content);
    return item;
  }

  async function renderForecast(location, peak, id) {
    const container = byId("live-forecast");
    const now = new Date();
    const days = (peak.getTime() - now.getTime()) / 86400000;
    const climate = finite(location.clear_probability_pct) ? `${number.format(location.clear_probability_pct)} %` : null;
    if (days < -0.25) {
      message(container, "El eclipse ya ha pasado", "No hay pronóstico que consultar para esta fecha.");
      return;
    }
    if (days > forecastDays - 1) {
      const from = new Date(peak.getTime() - (forecastDays - 1) * 86400000);
      message(container, `Faltan ${Math.ceil(days)} días`,
        `El pronóstico real se consultará aquí automáticamente a partir del ${longDate.format(from)}, cuando el eclipse entre en el horizonte de ${forecastDays} días del modelo.`
        + (climate ? ` Hasta entonces, la mejor referencia es la climatología: cielo despejado en el ${climate} de los años.` : " Hasta entonces, la referencia es la climatología histórica."));
      return;
    }
    if (!finite(location.latitude) || !finite(location.longitude)) {
      message(container, "Pronóstico no disponible", "Esta zona no tiene coordenadas.");
      return;
    }
    message(container, "Consultando el pronóstico…", "", "live-message loading");
    const params = new URLSearchParams({
      latitude: location.latitude.toFixed(4), longitude: location.longitude.toFixed(4),
      hourly: "cloud_cover,cloud_cover_low,precipitation_probability,precipitation,temperature_2m",
      timezone: "UTC", forecast_days: String(forecastDays)
    });
    let body;
    try {
      body = await fetchJson(`${forecastUrl}?${params}`, {}, 20000);
    } catch (error) {
      if (id !== request) return;
      message(container, "No se pudo consultar el pronóstico", "Comprueba la conexión y vuelve a elegir el destino. La climatología de la tarjeta sigue siendo válida como referencia.", "live-message error");
      return;
    }
    if (id !== request) return;
    const hourly = body && body.hourly;
    const times = hourly && Array.isArray(hourly.time) ? hourly.time.map((value) => new Date(`${value}:00Z`).getTime()) : [];
    if (!times.length) {
      message(container, "Pronóstico no disponible", "El servicio no devolvió datos para esta zona.", "live-message error");
      return;
    }
    // Hora del pronóstico más cercana al máximo del eclipse.
    let index = 0;
    times.forEach((time, position) => { if (Math.abs(time - peak.getTime()) < Math.abs(times[index] - peak.getTime())) index = position; });
    if (Math.abs(times[index] - peak.getTime()) > 90 * 60000) {
      message(container, "Pronóstico aún no disponible para esa hora", "El modelo todavía no cubre la hora del máximo. Vuelve a consultarlo en unas horas.");
      return;
    }
    const value = (name) => (hourly[name] && finite(hourly[name][index]) ? hourly[name][index] : null);
    const cloud = value("cloud_cover");
    const result = node("div");
    const verdict = cloud === null ? "Nubosidad no disponible" : cloud <= clearCloudCoverPct ? "Cielo despejado previsto" : cloud <= 60 ? "Nubes parciales previstas" : "Cielo cubierto previsto";
    result.append(node("p", `forecast-verdict${cloud !== null && cloud <= clearCloudCoverPct ? " good" : ""}`, verdict));
    const stats = node("dl", "climate-stats forecast-stats");
    const fmt = (v, unit) => (v === null ? "—" : `${number.format(v)}${unit}`);
    stats.append(
      forecastStat("Nubosidad", fmt(cloud, " %"), `Baja: ${fmt(value("cloud_cover_low"), " %")}`),
      forecastStat("Probabilidad de lluvia", fmt(value("precipitation_probability"), " %"), `Precipitación: ${fmt(value("precipitation"), " mm")}`),
      forecastStat("Temperatura", fmt(value("temperature_2m"), " °C"), "A 2 m del suelo")
    );
    result.append(stats);
    // Evolución de la nubosidad en las horas alrededor del máximo.
    const strip = node("ol", "cloud-strip");
    strip.setAttribute("aria-label", "Nubosidad prevista alrededor del máximo");
    for (let offset = -3; offset <= 3; offset += 1) {
      const position = index + offset;
      if (position < 0 || position >= times.length) continue;
      const cover = hourly.cloud_cover[position];
      const item = node("li", offset === 0 ? "peak" : "");
      const bar = node("span", "cloud-bar");
      bar.style.height = `${finite(cover) ? Math.max(4, cover) : 0}%`;
      item.append(bar, node("small", "", offset === 0 ? "Máx." : `${offset > 0 ? "+" : ""}${offset} h`));
      item.title = `${offset === 0 ? "Hora del máximo" : `${offset > 0 ? "+" : ""}${offset} h`}: ${finite(cover) ? `${number.format(cover)} % de nubes` : "sin dato"}`;
      strip.append(item);
    }
    result.append(strip);
    const reliability = days > 7 ? "A más de una semana el pronóstico es poco fiable; úsalo como tendencia." : days > 3 ? "Fiabilidad media: revísalo de nuevo en los próximos días." : "Pronóstico a corto plazo: es la mejor referencia disponible.";
    result.append(node("p", "live-note", `${reliability}${climate ? ` Climatología del lugar: despejado el ${climate} de los años.` : ""} Fuente: Open-Meteo, consultado ahora.`));
    container.replaceChildren(result);
  }

  // ---------- Alojamientos ----------

  function lodgingName(tags) {
    return tags.name || tags["name:es"] || tags["name:en"] || "";
  }

  function lodgingQuery(origin) {
    const types = Object.keys(lodgingTypes).join("|");
    return `[out:json][timeout:20];nwr["tourism"~"^(${types})$"](around:${origin.radius},${origin.lat.toFixed(5)},${origin.lon.toFixed(5)});out center tags 300;`;
  }

  function lodgingItem(origin, id, tags, itemLat, itemLon) {
    if (!finite(itemLat) || !finite(itemLon)) return null;
    const distance = distanceKm(origin.lat, origin.lon, itemLat, itemLon);
    if (distance > origin.radius / 1000) return null;
    const website = tags.website || tags["contact:website"] || "";
    const image = /^https:\/\/\S+\.(jpe?g|png|webp)$/i.test(tags.image || "") ? tags.image : "";
    return {
      id, name: lodgingName(tags), type: tags.tourism, stars: tags.stars || "", city: tags["addr:city"] || "",
      lat: itemLat, lon: itemLon, website: /^https?:\/\//i.test(website) ? website : "", image, distance
    };
  }

  async function fromOverpass(origin) {
    const body = new URLSearchParams({ data: lodgingQuery(origin) }).toString();
    const controllers = overpassUrls.map(() => new AbortController());
    const attempts = overpassUrls.map((url, position) => fetchJson(url, {
      method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" }, signal: controllers[position].signal
    }, 30000).then((json) => {
      if (!Array.isArray(json.elements)) throw new Error("Respuesta sin elementos");
      controllers.forEach((controller, other) => { if (other !== position) controller.abort(); });
      return json.elements;
    }));
    const elements = await Promise.any(attempts);
    return elements.map((element) => lodgingItem(origin, `${element.type}/${element.id}`, element.tags || {},
      finite(element.lat) ? element.lat : element.center && element.center.lat,
      finite(element.lon) ? element.lon : element.center && element.center.lon));
  }

  // Respaldo rápido: Photon busca por texto, así que solo encuentra alojamientos con «hotel» en el nombre.
  async function fromPhoton(origin) {
    const dLat = origin.radius / 111320;
    const dLon = dLat / Math.max(0.2, Math.cos(origin.lat * Math.PI / 180));
    const params = new URLSearchParams({
      q: "hotel", lat: origin.lat.toFixed(5), lon: origin.lon.toFixed(5), limit: "50",
      bbox: [origin.lon - dLon, origin.lat - dLat, origin.lon + dLon, origin.lat + dLat].map((v) => v.toFixed(5)).join(",")
    });
    Object.keys(lodgingTypes).forEach((type) => params.append("osm_tag", `tourism:${type}`));
    const json = await fetchJson(`${photonUrl}?${params}`, {}, 15000);
    return (Array.isArray(json.features) ? json.features : []).map((feature) => {
      const p = feature.properties || {};
      const [itemLon, itemLat] = feature.geometry && Array.isArray(feature.geometry.coordinates) ? feature.geometry.coordinates : [];
      return lodgingItem(origin, `${p.osm_type}/${p.osm_id}`, { name: p.name, tourism: p.osm_value, "addr:city": p.city }, itemLat, itemLon);
    });
  }

  async function fetchLodgings(lat, lon) {
    const key = `${lat.toFixed(4)},${lon.toFixed(4)}`;
    if (lodgingCache.has(key)) return lodgingCache.get(key);
    let result = await searchLodgings({ lat, lon, radius: lodgingRadiusM });
    if (result.items.length < minLodgings) result = await searchLodgings({ lat, lon, radius: wideLodgingRadiusM });
    lodgingCache.set(key, result);
    return result;
  }

  async function searchLodgings(origin) {
    let items;
    let complete = true;
    try {
      items = await fromOverpass(origin);
    } catch (error) {
      items = await fromPhoton(origin);
      complete = false;
    }
    // Un mismo alojamiento puede estar como punto y como edificio: se quitan los homónimos a menos de 200 m.
    const unique = [];
    items.filter(Boolean).sort((a, b) => a.distance - b.distance).forEach((item) => {
      const name = item.name.trim().toLocaleLowerCase("es");
      if (name && unique.some((other) => other.name.trim().toLocaleLowerCase("es") === name && distanceKm(other.lat, other.lon, item.lat, item.lon) < 0.2)) return;
      unique.push(item);
    });
    return { complete, items: unique, radius: origin.radius };
  }

  // Búsqueda de Booking con las fechas y personas elegidas.
  function bookingUrl(text, stay) {
    const [inYear, inMonth, inDay] = stay.checkin.split("-").map(Number);
    const [outYear, outMonth, outDay] = stay.checkout.split("-").map(Number);
    // Las fechas van en los dos formatos que entiende Booking: si la búsqueda lleva directamente a la
    // ficha de un hotel, conserva las del formato por campos.
    const params = new URLSearchParams({
      ss: text, checkin: stay.checkin, checkout: stay.checkout,
      checkin_year: String(inYear), checkin_month: String(inMonth), checkin_monthday: String(inDay),
      checkout_year: String(outYear), checkout_month: String(outMonth), checkout_monthday: String(outDay),
      group_adults: String(stay.adults), no_rooms: String(stay.rooms), group_children: "0", selected_currency: "EUR"
    });
    return `https://www.booking.com/searchresults.es.html?${params}`;
  }

  function isReference(location) {
    return location.candidate_type === "astronomical_reference" || /^Referencia\b/i.test(location.name || "");
  }

  function searchLinks(location, stay) {
    const container = byId("lodging-links");
    const lat = location.latitude.toFixed(5);
    const lon = location.longitude.toFixed(5);
    const links = [link("Ver hoteles y precios en Google Maps", `https://www.google.com/maps/search/hoteles/@${lat},${lon},13z`, "action-link primary")];
    if (!isReference(location)) {
      links.push(link("Disponibilidad en Booking", bookingUrl(`${location.name}, ${location.country_name || ""}`.replace(/,\s*$/, ""), stay), "action-link"));
    }
    container.replaceChildren(...links);
  }

  function hotelLinks(item, location, stay) {
    const area = item.city || (isReference(location) ? location.country_name || "" : location.name);
    const wrap = node("span", "lodging-actions");
    // Google Maps busca el nombre alrededor de las coordenadas del alojamiento, así no confunde homónimos.
    const at = `@${item.lat.toFixed(6)},${item.lon.toFixed(6)},17z`;
    wrap.append(link("Google Maps", item.name
      ? `https://www.google.com/maps/search/${encodeURIComponent(item.name)}/${at}`
      : `https://www.google.com/maps/search/?api=1&query=${item.lat.toFixed(6)},${item.lon.toFixed(6)}`));
    if (item.name) {
      wrap.append(link("Booking", bookingUrl(`${item.name}${area ? `, ${area}` : ""}`, stay)));
    }
    if (item.website) wrap.append(link("Web", item.website));
    return wrap;
  }

  function lodgingDescription(item) {
    const parts = [lodgingTypes[item.type] || "Alojamiento"];
    if (/^\d(\.\d)?S?$/i.test(item.stars)) parts.push(`${item.stars.replace(/S$/i, "")} ★`);
    parts.push(`${number.format(item.distance)} km`);
    return parts.join(" · ");
  }

  // Teselas de Esri: no exigen clave ni cabecera Referer, así que funcionan con la página abierta
  // como archivo local (OpenStreetMap y CARTO rechazan esas peticiones).
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services";
  const tileAttribution = 'Teselas &copy; <a href="https://www.esri.com/">Esri</a> · Alojamientos &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

  function currentTiles() {
    const root = document.documentElement;
    const dark = root.dataset.theme === "dark" || (!root.dataset.theme && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    return dark
      ? [`${esri}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, `${esri}/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`]
      : [`${esri}/World_Street_Map/MapServer/tile/{z}/{y}/{x}`];
  }

  function setTiles() {
    tileLayer.clearLayers();
    currentTiles().forEach((url, position) => tileLayer.addLayer(window.L.tileLayer(url, { maxZoom: 16, attribution: position ? "" : tileAttribution })));
  }

  function ensureMap(location) {
    const container = byId("lodging-map");
    if (!window.L) {
      container.hidden = true;
      return null;
    }
    container.hidden = false;
    if (!map) {
      // Rueda para acercar y alejar; Leaflet impide que la página se desplace con el cursor sobre el mapa.
      map = window.L.map(container, { scrollWheelZoom: true, wheelPxPerZoomLevel: 90 });
      map.setMaxZoom(16);
      tileLayer = window.L.layerGroup().addTo(map);
      setTiles();
      markerLayer = window.L.layerGroup().addTo(map);
      priceLayer = window.L.layerGroup().addTo(map);
      // El mapa sigue el modo día / noche de la página.
      new MutationObserver(setTiles).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
    markerLayer.clearLayers();
    priceLayer.clearLayers();
    markersById = new Map();
    const site = siteMarker = window.L.circleMarker([location.latitude, location.longitude], { radius: 9, color: "#c98026", weight: 3, fillColor: "#e9a93a", fillOpacity: 0.9 });
    site.bindPopup(node("strong", "", `${location.name || "Zona"}: punto de observación`));
    markerLayer.addLayer(site);
    map.setView([location.latitude, location.longitude], 11);
    setTimeout(() => map.invalidateSize(), 0);
    return map;
  }

  function addLodgingMarkers(items, location, stay, radius) {
    if (!map) return;
    const area = window.L.circle([location.latitude, location.longitude], { radius, color: "#176a65", weight: 1, fill: false, dashArray: "4 6" }).addTo(markerLayer);
    map.fitBounds(area.getBounds(), { padding: [10, 10] });
    items.forEach((item) => {
      const marker = window.L.circleMarker([item.lat, item.lon], { radius: 5, color: "#176a65", weight: 1.5, fillColor: "#5fc2b4", fillOpacity: 0.85 });
      const popup = node("div", "lodging-popup");
      popup.append(node("strong", "", item.name || "Alojamiento sin nombre"), node("span", "", lodgingDescription(item)), hotelLinks(item, location, stay));
      const pictures = node("div", "popup-photos");
      popup.prepend(pictures);
      marker.bindPopup(popup, { maxWidth: 280 });
      marker.on("popupopen", () => {
        if (pictures.dataset.loaded) return;
        pictures.dataset.loaded = "1";
        popupPhotos(item, isReference(location) ? item.city : location.name, pictures, marker);
      });
      markerLayer.addLayer(marker);
      markersById.set(item.id, marker);
    });
    if (siteMarker) siteMarker.bringToFront(); // el punto de observación, siempre visible sobre los alojamientos
  }

  async function renderLodgings(location, stay, id) {
    const status = byId("lodging-status");
    const list = byId("lodging-list");
    list.replaceChildren();
    if (!finite(location.latitude) || !finite(location.longitude)) {
      status.textContent = "Esta zona no tiene coordenadas para buscar alojamiento.";
      return;
    }
    searchLinks(location, stay);
    ensureMap(location);
    status.className = "lodging-status loading";
    status.textContent = "Buscando alojamientos en OpenStreetMap… puede tardar hasta medio minuto.";
    let result;
    try {
      result = await fetchLodgings(location.latitude, location.longitude);
    } catch (error) {
      if (id !== lodgingRequest) return;
      renderPrices(location, stay, id, lodgingRadiusM, []);
      status.className = "lodging-status error";
      status.textContent = "No se pudo consultar OpenStreetMap ahora mismo (el servicio público puede estar saturado o no hay conexión). Los enlaces de Google Maps y Booking funcionan igualmente; vuelve a elegir el destino para reintentarlo.";
      return;
    }
    if (id !== lodgingRequest) return;
    const items = result.items;
    renderPrices(location, stay, id, result.radius, items);
    const named = items.filter((item) => item.name);
    status.className = "lodging-status";
    const partial = result.complete ? "" : " Búsqueda parcial: el servicio principal no respondió y solo se muestran alojamientos con «hotel» en el nombre.";
    const km = result.radius / 1000;
    const widened = result.radius > lodgingRadiusM ? ` Se amplió la búsqueda a ${km} km porque cerca del punto apenas hay alojamientos registrados.` : "";
    if (!items.length) {
      addLodgingMarkers([], location, stay, result.radius);
      status.textContent = `OpenStreetMap no registra alojamientos a menos de ${km} km.${partial} Puede ser un hueco en los datos abiertos: usa los enlaces de búsqueda.`;
      return;
    }
    status.textContent = `${items.length} alojamientos en OpenStreetMap a menos de ${km} km (${named.length} con nombre). Es un mínimo: la cobertura de los datos abiertos varía según el país.${widened}${partial}`;
    addLodgingMarkers(items, location, stay, result.radius);
    named.slice(0, listedLodgings).forEach((item) => {
      const row = node("li");
      const title = node("button", "lodging-name", item.name);
      title.type = "button";
      title.addEventListener("click", () => {
        const marker = markersById.get(item.id);
        if (marker && map) {
          map.setView([item.lat, item.lon], Math.max(map.getZoom(), 14));
          marker.openPopup();
        }
      });
      row.append(title, node("span", "lodging-meta", lodgingDescription(item)), hotelLinks(item, location, stay));
      list.append(row);
    });
  }

  // ---------- Precios (solo con la app servida por src/app_server.py) ----------

  function priceServer() {
    if (!/^https?:$/.test(window.location.protocol)) return Promise.resolve(null);
    if (!serverStatus) serverStatus = fetchJson("/api/status", {}, 5000).catch(() => null);
    return serverStatus;
  }

  // Localidad que se busca en TripAdvisor: el nombre del destino o, si es una referencia NASA
  // sin nombre de lugar, la localidad más repetida entre los alojamientos de OpenStreetMap.
  function priceQueryName(location, items) {
    if (!isReference(location)) return location.name;
    const counts = new Map();
    items.forEach((item) => { if (item.city) counts.set(item.city, (counts.get(item.city) || 0) + 1); });
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([city]) => city)[0] || "";
  }

  function priceNote(box, title, text, className = "live-message") {
    const message = node("div", className);
    message.append(node("strong", "", title));
    if (text) message.append(node("p", "", text));
    box.replaceChildren(node("h5", "prices-heading", "Disponibilidad y precio para tus fechas"), message);
  }

  async function renderPrices(location, stay, id, radiusM, items) {
    const box = byId("lodging-prices");
    if (!box) return;
    box.replaceChildren();
    const status = await priceServer();
    if (id !== lodgingRequest) return;
    if (!status) {
      priceNote(box, "Precios dentro de la app", "La app está abierta como archivo. Para ver aquí la disponibilidad, el precio de tus fechas y la foto de TripAdvisor de cada alojamiento, ábrela con «abrir_planificador.bat» (o «python src/app_server.py»). Mientras, el enlace de Booking de cada alojamiento lleva a tus fechas y su ventana en el mapa muestra fotos del entorno.");
      return;
    }
    if (!status.prices) {
      priceNote(box, "Precios sin activar", "El servidor local no tiene clave de RapidAPI. Créala gratis y guárdala en la variable RAPIDAPI_KEY o en el archivo .rapidapi_key de tu carpeta de usuario (instrucciones en el README).");
      return;
    }
    const name = priceQueryName(location, items);
    if (!name) {
      priceNote(box, "Sin localidad para buscar precios", "Este punto no tiene nombre de localidad. Elige una zona cercana con nombre o usa los enlaces de búsqueda.");
      return;
    }
    priceNote(box, "Consultando disponibilidad y precios…", "Ofertas de Booking, Agoda y otras agencias para tus fechas y personas; puede tardar unos segundos.", "live-message loading");
    const params = new URLSearchParams({
      name, country: location.country_name || "", lat: location.latitude.toFixed(5), lon: location.longitude.toFixed(5),
      checkin: stay.checkin, checkout: stay.checkout, adults: String(stay.adults), rooms: String(stay.rooms),
      radius_km: String(Math.min(25, radiusM / 1000))
    });
    let body;
    try {
      body = await fetchJson(`/api/prices?${params}`, {}, 60000);
    } catch (error) {
      if (id !== lodgingRequest) return;
      priceNote(box, "No se pudieron consultar los precios", "El servicio de tarifas no respondió. Los enlaces de Google Maps y Booking siguen mostrando el precio real.", "live-message error");
      return;
    }
    if (id !== lodgingRequest) return;
    if (body.status === "no_key") {
      priceNote(box, "Precios sin activar", "Falta la clave de RapidAPI en el servidor local (instrucciones en el README).");
      return;
    }
    if (body.status === "no_location") {
      priceNote(box, "Localidad no encontrada", `TripAdvisor no reconoce «${name}». Usa los enlaces de búsqueda.`);
      return;
    }
    if (body.status === "no_hotels") {
      priceNote(box, "Sin alojamientos con precio cerca", `TripAdvisor no tiene alojamientos de «${body.location && body.location.name || name}» a menos de ${Math.min(25, radiusM / 1000)} km de este punto.`);
      return;
    }
    if (body.status !== "ok") {
      priceNote(box, "Precios no disponibles", "Los enlaces de Google Maps y Booking siguen mostrando el precio real.", "live-message error");
      return;
    }
    const hasPrice = (hotel) => hotel.price && finite(hotel.price.min);
    // Orden: tarifas normales de menor a mayor, después las atípicas y al final los que no tienen tarifa.
    const rank = (hotel) => (!hasPrice(hotel) ? 2 : hotel.price.outlier ? 1 : 0);
    const hotels = body.hotels.slice().sort((a, b) => rank(a) - rank(b) || (hasPrice(a) ? a.price.min : 0) - (hasPrice(b) ? b.price.min : 0) || a.distance_km - b.distance_km);
    const priced = hotels.filter((hotel) => hasPrice(hotel) && !hotel.price.outlier);
    const outliers = hotels.filter((hotel) => hasPrice(hotel) && hotel.price.outlier).length;
    const nights = body.nights;
    const group = `${body.adults} ${body.adults === 1 ? "adulto" : "adultos"}, ${body.rooms} ${body.rooms === 1 ? "habitación" : "habitaciones"}`;
    const range = `${shortDate(stay.checkin)}–${shortDate(stay.checkout)}`;
    const perNight = body.rooms > 1 ? "por habitación y noche" : "por noche";
    const heading = node("h5", "prices-heading", `Disponibilidad y precio · ${range} · ${nights} ${nights === 1 ? "noche" : "noches"} · ${group}`);
    const summary = node("p", "lodging-status", priced.length
      ? `Disponibles ${priced.length} de los ${hotels.length} alojamientos consultados (de ${body.total_nearby} en TripAdvisor a menos de ${Math.min(25, radiusM / 1000)} km), desde ${euro.format(priced[0].price.min)} ${perNight} (${euro.format(priced[0].price.total)} en total)${outliers ? `; ${outliers} más con un precio atípico` : ""}. Pulsa un nombre para ver sus fotos.`
      : "Ninguno de los alojamientos más cercanos publica todavía una oferta fiable para esas fechas. Es habitual a más de un año vista: vuelve a consultarlo más adelante o prueba otras fechas.");
    const far = finite(body.days_ahead) && body.days_ahead > 330
      ? node("p", "live-message error", `Faltan ${body.days_ahead} días: muchos alojamientos aún no han abierto reservas para esas fechas y algunas agencias devuelven tarifas provisionales o desproporcionadas. Tómalas como orientación y compruébalas en la agencia.`)
      : null;
    const list = node("ol", "price-list");
    const unpriced = hotels.filter((hotel) => !hasPrice(hotel));
    const area = isReference(location) ? name : location.name;
    hotels.filter(hasPrice).forEach((hotel) => {
      const row = node("li", hotel.price.outlier ? "suspect" : "");
      const info = node("div", "price-info");
      const meta = [hotel.type, finite(hotel.rating) && hotel.rating > 0 ? `${number.format(hotel.rating)} ★ (${hotel.reviews || 0})` : "", `${number.format(hotel.distance_km)} km`].filter(Boolean).join(" · ");
      const title = node("button", "lodging-name", hotel.name);
      title.type = "button";
      title.setAttribute("aria-expanded", "false");
      const badge = hotel.price.outlier
        ? node("span", "availability doubtful", "Por confirmar")
        : node("span", "availability", "Disponible");
      const titleLine = node("span", "price-title");
      titleLine.append(title, badge);
      info.append(titleLine, node("span", "lodging-meta", meta));
      const actions = node("span", "lodging-actions");
      actions.append(link("Reservar en Booking", bookingUrl(`${hotel.name}${area ? `, ${area}` : ""}`, stay)));
      if (/^https:\/\/www\.tripadvisor\./.test(hotel.url)) actions.append(link("TripAdvisor", hotel.url));
      info.append(actions);
      const price = node("div", "price-value");
      price.append(node("strong", "", euro.format(hotel.price.min)), node("small", "", `${perNight} · ${hotel.price.provider}`));
      if (finite(hotel.price.total)) price.append(node("small", "price-total", `${euro.format(hotel.price.total)} en total`));
      if (hotel.price.outlier) price.append(node("small", "suspect-note", "Precio atípico: verifícalo"));
      price.title = hotel.price.offers.map((offer) => `${offer.provider}: ${euro.format(offer.rate)}`).join("\n");
      const photos = node("div", "hotel-photos");
      photos.hidden = true;
      title.addEventListener("click", () => {
        const open = photos.hidden;
        photos.hidden = !open;
        title.setAttribute("aria-expanded", String(open));
        if (open && !photos.childElementCount) renderPhotos(hotel, area, photos);
        if (open && map) map.setView([hotel.lat, hotel.lon], Math.max(map.getZoom(), 14));
      });
      row.append(info, price, photos);
      list.append(row);
      if (map && priceLayer && !hotel.price.outlier) {
        const pin = window.L.marker([hotel.lat, hotel.lon], { icon: window.L.divIcon({ className: "price-pin-anchor", html: "", iconSize: [0, 0] }) });
        pin.on("add", () => {
          const element = pin.getElement();
          if (element && !element.firstChild) element.append(node("span", "price-pin", euro.format(hotel.price.min)));
        });
        const popup = node("div", "lodging-popup");
        if (hotel.image) popup.append(photo(tripadvisorImage(hotel.image, 300), `Foto de ${hotel.name}`, "popup-photo"));
        popup.append(node("strong", "", hotel.name), node("span", "", `${euro.format(hotel.price.min)} ${perNight} · ${euro.format(hotel.price.total)} en total · ${hotel.price.provider}`), node("span", "", meta));
        const more = node("button", "popup-more", "Ver fotos y reservar");
        more.type = "button";
        more.addEventListener("click", () => {
          if (photos.hidden) title.click();
          row.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
        popup.append(more);
        pin.bindPopup(popup);
        priceLayer.addLayer(pin);
      }
    });
    const note = node("p", "live-note", `«Disponible» significa que al menos una de las agencias que recoge Xotelo a partir de TripAdvisor (Booking, Agoda, Expedia…) ofrece habitación para esas fechas y personas; no garantiza la reserva. Tarifa más baja ${perNight}, consultada ahora (${new Date(body.retrieved_utc).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" })}); puede no incluir impuestos y cambiar al reservar.`);
    // Los que no publican tarifa se resumen en una línea para no alargar la lista. Sin oferta no
    // distingue «completo» de «aún sin precio»: para fechas lejanas suele ser lo segundo.
    const rest = unpriced.length ? node("p", "lodging-meta unpriced", `Sin oferta para esas fechas: ${unpriced.slice(0, 8).map((hotel) => hotel.name).join(", ")}${unpriced.length > 8 ? ` y ${unpriced.length - 8} más` : ""}.`) : null;
    box.replaceChildren(...[heading, summary, far, list.children.length ? list : null, rest, note].filter(Boolean));
  }

  // ---------- Fotos del alojamiento ----------

  function shortDate(day) {
    return day.split("-").reverse().join("/");
  }

  // La foto de TripAdvisor llega a través del servidor local, que la pide al CDN ya redimensionada
  // (300 o 500 px de ancho, de los tamaños que admite).
  function tripadvisorImage(url, width) {
    return `/api/photo?${new URLSearchParams({ url: url.split("?")[0], w: String(width) })}`;
  }

  function photo(src, alt, className = "") {
    const image = node("img", className);
    image.src = src;
    image.alt = alt;
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => {
      const figure = image.closest("figure");
      (figure || image).remove();
    });
    return image;
  }

  function figure(src, alt, href, credit) {
    const item = node("figure", "photo");
    const anchor = link("", href);
    anchor.append(photo(src, alt));
    item.append(anchor, node("figcaption", "", credit));
    return item;
  }

  function plainText(html) {
    return new DOMParser().parseFromString(String(html || ""), "text/html").body.textContent.trim();
  }

  const nameWords = (text) => (text.toLocaleLowerCase("es").normalize("NFD").replace(/[̀-ͯ]/g, "").match(/[a-z0-9]{4,}/g) || [])
    .filter((word) => !["hotel", "hostal", "apartamento", "apartamentos", "suites", "resort", "spain", "espana"].includes(word));

  // Fotos de Wikimedia Commons tomadas a menos de 150 m del alojamiento. Primero las que citan su
  // nombre (suelen ser del propio edificio) y después las del entorno. Las palabras de la localidad
  // («Málaga» en «AC Hotel Málaga Palacio») no cuentan: aparecen en casi todas las fotos del lugar.
  async function commonsPhotos(hotel, area) {
    const key = `${hotel.lat.toFixed(5)},${hotel.lon.toFixed(5)}`;
    if (photoCache.has(key)) return photoCache.get(key);
    const params = new URLSearchParams({
      action: "query", format: "json", origin: "*", generator: "geosearch", ggscoord: `${hotel.lat}|${hotel.lon}`,
      ggsradius: String(commonsRadiusM), ggsnamespace: "6", ggslimit: "20", prop: "imageinfo",
      iiprop: "url|mime|extmetadata", iiurlwidth: "360", iiextmetadatafilter: "Artist|LicenseShortName"
    });
    const body = await fetchJson(`${commonsUrl}?${params}`, {}, 15000);
    const placeWords = nameWords(area || "");
    const words = nameWords(hotel.name).filter((word) => !placeWords.includes(word));
    const pages = Object.values((body.query && body.query.pages) || {});
    const photos = pages.map((page) => {
      const info = (page.imageinfo || [])[0] || {};
      const title = (page.title || "").replace(/^File:/, "").replace(/\.[a-z]+$/i, "");
      const meta = info.extmetadata || {};
      return {
        title, thumb: info.thumburl, href: info.descriptionurl, mime: info.mime,
        own: words.some((word) => nameWords(title).includes(word)),
        author: plainText(meta.Artist && meta.Artist.value).slice(0, 60),
        license: plainText(meta.LicenseShortName && meta.LicenseShortName.value)
      };
    }).filter((item) => item.thumb && /^image\/(jpeg|png|webp)$/.test(item.mime || ""))
      .sort((a, b) => Number(b.own) - Number(a.own)).slice(0, 6);
    photoCache.set(key, photos);
    return photos;
  }

  // Fotos en la ventana de un alojamiento de OpenStreetMap: la suya si la tiene etiquetada y las de
  // Wikimedia Commons de alrededor. No necesitan el servidor local.
  async function popupPhotos(item, area, container, marker) {
    const query = `${item.name || "hotel"}${area ? ` ${area}` : ""}`;
    const more = link("Más fotos en Google Imágenes", `https://www.google.com/search?${new URLSearchParams({ tbm: "isch", q: query })}`, "popup-more-photos");
    const strip = node("div", "popup-strip");
    if (item.image) strip.append(photo(item.image, `Foto de ${item.name}`));
    container.replaceChildren(strip, node("small", "lodging-meta", "Buscando fotos…"));
    let found = [];
    try {
      found = await commonsPhotos({ lat: item.lat, lon: item.lon, name: item.name || "" }, area);
    } catch (error) {
      found = [];
    }
    found.slice(0, item.image ? 2 : 3).forEach((picture) => {
      const anchor = link("", picture.href);
      anchor.title = `${picture.own ? "Alojamiento" : "Alrededores"} · ${picture.author || "Wikimedia Commons"}${picture.license ? ` · ${picture.license}` : ""}`;
      anchor.append(photo(picture.thumb, picture.title));
      strip.append(anchor);
    });
    const caption = strip.childElementCount
      ? node("small", "lodging-meta", `Fotos del alojamiento y de su entorno (Wikimedia Commons, a menos de ${commonsRadiusM} m).`)
      : node("small", "lodging-meta", "Sin fotos a su alrededor en Wikimedia Commons.");
    container.replaceChildren(...[strip.childElementCount ? strip : null, caption, more].filter(Boolean));
    if (marker && marker.getPopup()) marker.getPopup().update();
  }

  async function renderPhotos(hotel, area, container) {
    const query = `${hotel.name}${area ? ` ${area}` : ""}`;
    const links = node("p", "lodging-actions photo-links");
    links.append(link("Más fotos en Google Imágenes", `https://www.google.com/search?${new URLSearchParams({ tbm: "isch", q: query })}`));
    if (/^https:\/\/www\.tripadvisor\./.test(hotel.url)) links.append(link("Todas las fotos en TripAdvisor", hotel.url));
    const gallery = node("div", "photo-strip");
    if (hotel.image) {
      gallery.append(figure(tripadvisorImage(hotel.image, 500), `Foto de ${hotel.name}`, /^https:\/\/www\.tripadvisor\./.test(hotel.url) ? hotel.url : hotel.image, "Alojamiento · TripAdvisor"));
    }
    const status = node("p", "lodging-meta", "Buscando fotos del entorno en Wikimedia Commons…");
    container.replaceChildren(gallery, status, links);
    try {
      const photos = await commonsPhotos(hotel, area);
      photos.forEach((item) => gallery.append(figure(item.thumb, item.title, item.href,
        `${item.own ? "Alojamiento" : "Alrededores"} · ${item.author || "Wikimedia Commons"}${item.license ? ` · ${item.license}` : ""}`)));
      status.textContent = photos.length
        ? `Fotos de TripAdvisor y de Wikimedia Commons tomadas a menos de ${commonsRadiusM} m; pulsa una para ver su autor y licencia.`
        : `${hotel.image ? "" : "Este alojamiento no tiene foto en TripAdvisor. "}Wikimedia Commons no tiene fotos a menos de ${commonsRadiusM} m: usa los enlaces para ver más.`;
    } catch (error) {
      status.textContent = "No se pudieron cargar las fotos del entorno ahora mismo; los enlaces muestran más fotos.";
    }
  }

  // ---------- Fechas y personas ----------

  function today() {
    return new Date().toISOString().slice(0, 10);
  }

  function nightsBetween(checkin, checkout) {
    return Math.round((new Date(`${checkout}T00:00:00Z`) - new Date(`${checkin}T00:00:00Z`)) / 86400000);
  }

  // Comprueba el formulario y devuelve la estancia, o un mensaje si no es válida.
  function readStay(values) {
    const { checkin, checkout } = values;
    const adults = Number(values.adults);
    const rooms = Number(values.rooms);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(checkin || "") || !/^\d{4}-\d{2}-\d{2}$/.test(checkout || "")) return { error: "Indica las fechas de entrada y salida." };
    if (checkin < today()) return { error: "La fecha de entrada ya ha pasado." };
    const nights = nightsBetween(checkin, checkout);
    if (nights < 1) return { error: "La salida debe ser posterior a la entrada." };
    if (nights > maxNights) return { error: `La estancia puede ser de ${maxNights} noches como máximo.` };
    if (!Number.isInteger(adults) || adults < 1 || adults > maxAdults) return { error: `Indica entre 1 y ${maxAdults} adultos.` };
    if (!Number.isInteger(rooms) || rooms < 1 || rooms > Math.min(maxRooms, adults)) return { error: `Indica entre 1 y ${Math.min(maxRooms, adults)} habitaciones (no más que adultos).` };
    return { stay: { checkin, checkout, adults, rooms } };
  }

  function fillStayForm(stay) {
    const form = byId("stay-form");
    if (!form) return;
    byId("stay-checkin").value = stay.checkin;
    byId("stay-checkout").value = stay.checkout;
    byId("stay-adults").value = String(stay.adults);
    byId("stay-rooms").value = String(stay.rooms);
    byId("stay-checkin").min = today();
    byId("stay-checkout").min = shiftDay(stay.checkin, 1);
    byId("stay-error").hidden = true;
  }

  function subtitle(day, stay) {
    const nights = nightsBetween(stay.checkin, stay.checkout);
    return `Eclipse el ${longDate.format(new Date(`${day}T00:00:00Z`))} (fecha local). Alojamiento del ${longDate.format(new Date(`${stay.checkin}T00:00:00Z`))} al ${longDate.format(new Date(`${stay.checkout}T00:00:00Z`))}: ${nights} ${nights === 1 ? "noche" : "noches"}, ${stay.adults} ${stay.adults === 1 ? "adulto" : "adultos"}.`;
  }

  function bindStayForm() {
    const form = byId("stay-form");
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    byId("stay-checkin").addEventListener("change", () => {
      const checkin = byId("stay-checkin").value;
      if (!checkin) return;
      byId("stay-checkout").min = shiftDay(checkin, 1);
      // Si la salida queda antes de la nueva entrada, se mantiene el mismo número de noches.
      if (byId("stay-checkout").value <= checkin && current) {
        byId("stay-checkout").value = shiftDay(checkin, Math.max(1, nightsBetween(current.stay.checkin, current.stay.checkout)));
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!current) return;
      const result = readStay({
        checkin: byId("stay-checkin").value, checkout: byId("stay-checkout").value,
        adults: byId("stay-adults").value, rooms: byId("stay-rooms").value
      });
      const error = byId("stay-error");
      error.hidden = !result.error;
      error.textContent = result.error || "";
      if (result.error) return;
      current.stay = result.stay;
      byId("destination-subtitle").textContent = subtitle(current.day, current.stay);
      lodgingRequest += 1;
      renderLodgings(current.location, current.stay, lodgingRequest);
    });
  }

  // ---------- Panel ----------

  function show(location, event) {
    const panel = byId("destination-panel");
    if (!panel || !location) return;
    request += 1;
    lodgingRequest += 1;
    const id = request;
    const peak = new Date(location.peak_time_utc || `${event.event_date}T12:00:00Z`);
    const day = localDay(Number.isNaN(peak.getTime()) ? new Date(`${event.event_date}T12:00:00Z`) : peak, location.timezone);
    // Por defecto, dos noches: la anterior y la del eclipse, válidas para eclipses de día y de noche.
    // Se conservan las personas que ya se hubieran indicado para otro destino.
    const people = current ? { adults: current.stay.adults, rooms: current.stay.rooms } : { adults: 2, rooms: 1 };
    const stay = { checkin: shiftDay(day, -1), checkout: shiftDay(day, 1), ...people };
    current = { location, day, stay };
    panel.hidden = false;
    bindStayForm();
    fillStayForm(stay);
    byId("destination-title").textContent = `${location.name || "Zona"}${location.country_name ? ` · ${location.country_name}` : ""}`;
    byId("destination-subtitle").textContent = subtitle(day, stay);
    renderForecast(location, Number.isNaN(peak.getTime()) ? new Date(`${event.event_date}T12:00:00Z`) : peak, id);
    renderLodgings(location, stay, lodgingRequest);
  }

  function hide() {
    request += 1;
    lodgingRequest += 1;
    const panel = byId("destination-panel");
    if (panel) panel.hidden = true;
  }

  return { show, hide, readStay, bookingUrl };
})();
