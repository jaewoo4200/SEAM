/**
 * Interactive OpenStreetMap area picker for the OSM import flow.
 *
 * Shows a real OSM map (Leaflet + openstreetmap.org tiles — needs internet,
 * same as the import itself). Pan/zoom to the site, hit "Select area", then
 * DRAG a rectangle; the center lat/lon and width/height (m) flow back to the
 * numeric fields (which stay editable — edits re-draw the rectangle here).
 */

import { useEffect, useRef } from "react";
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

  return <div ref={divRef} className="osm-map" />;
}
