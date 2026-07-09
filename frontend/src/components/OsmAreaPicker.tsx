/**
 * Interactive OpenStreetMap area picker for the OSM import flow.
 *
 * Shows a real OSM map (Leaflet + openstreetmap.org tiles — needs internet,
 * same as the import itself). Pan/zoom to the site, hit "Select area", then
 * DRAG a rectangle; the center lat/lon and width/height (m) flow back to the
 * numeric fields (which stay editable — edits re-draw the rectangle here).
 */

import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

// Meters per degree (WGS-84 mean; matches the backend's ENU projection).
const M_PER_DEG_LAT = 110540.0;
const M_PER_DEG_LON_EQ = 111320.0;

export interface OsmArea {
  lat: number;
  lon: number;
  widthM: number;
  heightM: number;
}

/** One geocoding hit from Nominatim's jsonv2 response (only fields we use). */
interface NominatimHit {
  lat: string;
  lon: string;
  display_name: string;
  place_id: number;
}

function areaToBounds(a: OsmArea): L.LatLngBoundsExpression {
  const dLat = a.heightM / 2 / M_PER_DEG_LAT;
  const dLon = a.widthM / 2 / (M_PER_DEG_LON_EQ * Math.cos((a.lat * Math.PI) / 180));
  return [
    [a.lat - dLat, a.lon - dLon],
    [a.lat + dLat, a.lon + dLon],
  ];
}

export default function OsmAreaPicker({
  area,
  selecting,
  onArea,
}: {
  area: OsmArea;
  /** True while the "Select area" mode is armed (dragging draws the rect). */
  selecting: boolean;
  onArea: (a: OsmArea) => void;
}) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const rectRef = useRef<L.Rectangle | null>(null);
  const selectingRef = useRef(selecting);
  selectingRef.current = selecting;
  const onAreaRef = useRef(onArea);
  onAreaRef.current = onArea;

  // --- Location search (Nominatim geocoding) ---
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<NominatimHit[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  async function runSearch() {
    const q = query.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    setSearched(true);
    setOpen(true);
    try {
      // Nominatim usage policy forbids autocomplete-style spam, so we geocode
      // on EXPLICIT submit only (Enter / button click) — never per keystroke.
      // Browsers can't set User-Agent; Nominatim identifies the request by the
      // browser-supplied UA + Referer, which is fine for jsonv2 fetches.
      const url =
        "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q=" +
        encodeURIComponent(q);
      const res = await fetch(url, {
        headers: { "Accept-Language": navigator.language },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: NominatimHit[] = await res.json();
      setResults(Array.isArray(data) ? data.slice(0, 5) : []);
    } catch {
      setError("Search failed — check your connection and try again.");
      setResults([]);
    } finally {
      setBusy(false);
    }
  }

  // Clicking a result pans/zooms the map there and closes the dropdown. It does
  // NOT touch the selection rectangle — the user still drags or types W/H.
  function goToResult(hit: NominatimHit) {
    const map = mapRef.current;
    const lat = Number(hit.lat);
    const lon = Number(hit.lon);
    if (map && Number.isFinite(lat) && Number.isFinite(lon)) {
      map.setView([lat, lon], 16);
    }
    setOpen(false);
  }

  // Map bootstrap (once).
  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    const map = L.map(divRef.current, { zoomControl: true, attributionControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    map.fitBounds(areaToBounds(area), { maxZoom: 16 });

    // Drag-to-select: while armed, mousedown starts a rectangle, mousemove
    // grows it, mouseup commits center + W/H back to the form.
    let start: L.LatLng | null = null;
    let draft: L.Rectangle | null = null;
    map.on("mousedown", (e: L.LeafletMouseEvent) => {
      if (!selectingRef.current) return;
      start = e.latlng;
      map.dragging.disable();
      draft = L.rectangle(L.latLngBounds(start, start), {
        color: "#4fc3f7",
        weight: 2,
        fillOpacity: 0.12,
        dashArray: "6 4",
      }).addTo(map);
      L.DomEvent.stop(e.originalEvent);
    });
    map.on("mousemove", (e: L.LeafletMouseEvent) => {
      if (!start || !draft) return;
      draft.setBounds(L.latLngBounds(start, e.latlng));
    });
    const finish = (e?: L.LeafletMouseEvent) => {
      if (!start || !draft) return;
      const b = draft.getBounds();
      draft.remove();
      draft = null;
      start = null;
      map.dragging.enable();
      const c = b.getCenter();
      const heightM = (b.getNorth() - b.getSouth()) * M_PER_DEG_LAT;
      const widthM =
        (b.getEast() - b.getWest()) *
        M_PER_DEG_LON_EQ *
        Math.cos((c.lat * Math.PI) / 180);
      // Ignore accidental clicks (sub-20 m rectangles).
      if (widthM > 20 && heightM > 20) {
        onAreaRef.current({
          lat: Math.round(c.lat * 1e6) / 1e6,
          lon: Math.round(c.lng * 1e6) / 1e6,
          widthM: Math.round(widthM),
          heightM: Math.round(heightM),
        });
      }
      if (e) L.DomEvent.stop(e.originalEvent);
    };
    map.on("mouseup", finish);
    map.on("mouseout", () => {
      // Leaving the map mid-drag commits what we have (mirrors Leaflet.draw).
      if (start && draft) finish();
    });

    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      rectRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the committed rectangle in sync with the (editable) numeric fields.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !Number.isFinite(area.lat) || !Number.isFinite(area.lon)) return;
    const bounds = areaToBounds(area);
    if (!rectRef.current) {
      rectRef.current = L.rectangle(bounds, {
        color: "#4fc3f7",
        weight: 2,
        fillOpacity: 0.08,
      }).addTo(map);
    } else {
      rectRef.current.setBounds(bounds);
    }
    if (!map.getBounds().contains(bounds)) map.fitBounds(bounds, { maxZoom: 16 });
  }, [area.lat, area.lon, area.widthM, area.heightM]);

  // Cursor hint while select mode is armed.
  useEffect(() => {
    const el = divRef.current;
    if (el) el.style.cursor = selecting ? "crosshair" : "";
  }, [selecting]);

  return (
    <div className="osm-picker">
      <div className="osm-search">
        <input
          type="text"
          className="osm-search-input"
          placeholder="Search a place… (e.g. 한양대학교, Times Square)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void runSearch();
            } else if (e.key === "Escape") {
              setOpen(false);
            }
          }}
          onFocus={() => {
            if (results.length || busy || error) setOpen(true);
          }}
          // Delay close so a result click (mousedown) registers before blur.
          onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        />
        <button
          type="button"
          className="osm-search-btn"
          onClick={() => void runSearch()}
          disabled={busy || !query.trim()}
          title="Search"
        >
          <svg
            width={14}
            height={14}
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
            strokeLinecap="round"
            aria-hidden
          >
            <circle cx="7" cy="7" r="4.4" />
            <path d="M10.4 10.4 14 14" />
          </svg>
        </button>
        {open && (
          <div className="osm-search-results">
            {busy ? (
              <div className="osm-search-msg">Searching…</div>
            ) : error ? (
              <div className="osm-search-msg osm-search-err">{error}</div>
            ) : results.length === 0 ? (
              searched && <div className="osm-search-msg">No results.</div>
            ) : (
              results.map((hit) => (
                <button
                  key={hit.place_id}
                  type="button"
                  className="osm-search-item"
                  title={hit.display_name}
                  // mousedown (not click) so it fires before the input's blur.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    goToResult(hit);
                  }}
                >
                  {hit.display_name.length > 70
                    ? hit.display_name.slice(0, 70) + "…"
                    : hit.display_name}
                </button>
              ))
            )}
          </div>
        )}
      </div>
      <div ref={divRef} className="osm-map" />
    </div>
  );
}
