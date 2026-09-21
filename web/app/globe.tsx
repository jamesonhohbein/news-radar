"use client";

import * as maplibregl from "maplibre-gl";
import type { Map as MLMap, MapLayerMouseEvent } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";
import { CAMEO_ROOT, QUAD_CLASS } from "@/lib/cameo";

const STYLE = process.env.NEXT_PUBLIC_MAP_STYLE ?? "https://tiles.openfreemap.org/styles/positron";
const HOURS = 24;
const MIN_MENTIONS = 50;

type Region = { region: string; name: string | null; z: number; mentions: number; expected: number; peak_z: number | null };
type Ev = {
  id: number; added_at: string; cameo_root: string | null; quad_class: number | null;
  actor1_name: string | null; actor2_name: string | null; geo_name: string | null; country: string;
  lat: number; lon: number; num_mentions: number; num_sources: number; url: string | null;
};
type Daily = { day: string; mentions: number };

declare global { interface Window { __newsradar?: { ready: boolean; layers: string[]; regions: number; events: number } } }

export default function Globe() {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const namesRef = useRef<Record<string, string>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [events, setEvents] = useState<Ev[]>([]);
  const [hover, setHover] = useState<{ fips: string; name: string } | null>(null);
  const [series, setSeries] = useState<Daily[]>([]);

  useEffect(() => {
    if (!el.current || mapRef.current) return;
    const map = new maplibregl.Map({ container: el.current, style: STYLE, center: [10, 20], zoom: 1.9, attributionControl: { compact: true } });
    mapRef.current = map;
    (window as unknown as { __map: MLMap }).__map = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    map.on("style.load", async () => {
      map.setProjection({ type: "globe" });
      const [countries, anomaly, top] = await Promise.all([
        fetch("/countries.geojson").then((r) => r.json()),
        fetch("/api/anomaly?kind=country").then((r) => r.json()),
        fetch(`/api/events/top?hours=${HOURS}&limit=300`).then((r) => r.json()),
      ]);
      const regs: Region[] = anomaly.regions;
      const evs: Ev[] = top.events;
      setRegions(regs);
      setEvents(evs);
      for (const r of regs) if (r.name) namesRef.current[r.region] = r.name;
      for (const f of countries.features) namesRef.current[f.properties.fips] = f.properties.name;

      map.addSource("countries", { type: "geojson", data: countries, promoteId: "fips" });
      map.addLayer({
        id: "countries-fill", type: "fill", source: "countries",
        paint: {
          // z below 1 is invisible; 6 and up is the full tint. Peak z over the window.
          "fill-color": ["interpolate", ["linear"], ["coalesce", ["feature-state", "z"], 0], 1, "rgba(200,30,30,0)", 5, "rgba(200,30,30,0.85)"],
          "fill-opacity": 1,
        },
      }, firstSymbolLayer(map));
      map.addLayer({ id: "countries-line", type: "line", source: "countries", paint: { "line-color": "rgba(128,128,128,0.35)", "line-width": 0.5 } }, firstSymbolLayer(map));
      // Tint only regions with enough volume for z to mean something: a
      // microstate going from 1 mention to 25 scores z 24 and would glow.
      for (const r of regs) map.setFeatureState({ source: "countries", id: r.region }, { z: r.mentions >= MIN_MENTIONS ? r.z : 0, mentions: r.mentions });

      map.addSource("events", {
        type: "geojson",
        data: { type: "FeatureCollection", features: evs.map((e) => ({ type: "Feature", id: e.id, geometry: { type: "Point", coordinates: [e.lon, e.lat] }, properties: e })) },
      });
      map.addLayer({
        id: "events", type: "circle", source: "events",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "num_sources"], 1, 3, 50, 14],
          "circle-color": "#1d4ed8", "circle-opacity": 0.55, "circle-stroke-color": "#fff", "circle-stroke-width": 0.5,
        },
      });

      let hovered: string | null = null;
      map.on("mousemove", "countries-fill", (e: MapLayerMouseEvent) => {
        const f = e.features?.[0];
        const fips = f?.id as string | undefined;
        if (!fips || fips === hovered) return;
        hovered = fips;
        setHover({ fips, name: namesRef.current[fips] ?? fips });
      });
      map.on("mouseleave", "countries-fill", () => { hovered = null; });
      map.on("click", "events", (e: MapLayerMouseEvent) => {
        const p = e.features?.[0]?.properties as Ev | undefined;
        if (!p) return;
        new maplibregl.Popup({ closeButton: true }).setLngLat([p.lon, p.lat]).setHTML(popupHtml(p)).addTo(map);
      });
      map.on("mouseenter", "events", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "events", () => { map.getCanvas().style.cursor = ""; });

      window.__newsradar = { ready: true, layers: ["countries-fill", "events"], regions: regs.length, events: evs.length };
    });
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  useEffect(() => {
    if (!hover) return;
    let live = true;
    fetch(`/api/attention/daily?kind=country&region=${hover.fips}&days=30`).then((r) => r.json()).then((d) => { if (live) setSeries(d.series); });
    return () => { live = false; };
  }, [hover]);

  const hr = hover ? regions.find((r) => r.region === hover.fips) : undefined;
  const top = regions.filter((r) => r.mentions >= MIN_MENTIONS).slice(0, 8);

  return (
    <>
      <div id="map" ref={el} />
      <div className="panel">
        <h1>news-radar</h1>
        <div className="muted">Last {HOURS} h. Tint: share of world mentions vs the region&apos;s usual share over 30 days, as a z-score, regions with {MIN_MENTIONS}+ mentions. Dots: top {events.length} stories by first-window sources.</div>
        <div className="legend"><i /> z 1 → 5+ <b /> story</div>
        {hover ? (
          <>
            <table><tbody>
              <tr><td><strong>{hover.name}</strong></td><td className="num">{hr ? `z ${hr.z.toFixed(1)} · ${hr.mentions} vs ${hr.expected} expected` : "no activity"}</td></tr>
            </tbody></table>
            <Spark data={series} />
            <div className="muted">Daily mentions, 30 days</div>
          </>
        ) : (
          <table><tbody>
            {top.map((r) => (
              <tr key={r.region}><td>{namesRef.current[r.region] ?? r.region}</td><td className="num">z {r.z.toFixed(1)} · {r.mentions}</td></tr>
            ))}
          </tbody></table>
        )}
      </div>
    </>
  );
}

function firstSymbolLayer(map: MLMap): string | undefined {
  return map.getStyle().layers.find((l) => l.type === "symbol")?.id;
}

function popupHtml(p: Ev): string {
  const esc = (s: string | null | undefined) => (s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  const root = p.cameo_root ? CAMEO_ROOT[p.cameo_root] ?? p.cameo_root : "";
  const quad = p.quad_class ? QUAD_CLASS[p.quad_class] : "";
  const who = [p.actor1_name, p.actor2_name].filter(Boolean).map(esc).join(" → ") || "(unnamed actors)";
  const host = p.url ? (() => { try { return new URL(p.url!).hostname; } catch { return p.url; } })() : "";
  return `<div><strong>${who}</strong><br>${esc(root)}${quad ? ` · ${esc(quad)}` : ""}<br>${esc(p.geo_name)}<br>` +
    `<span style="color:#666">${p.num_sources} sources · ${p.num_mentions} mentions · ${new Date(p.added_at).toUTCString().slice(5, 22)} UTC</span>` +
    (p.url ? `<br><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(host)}</a>` : "") + `</div>`;
}

function Spark({ data }: { data: Daily[] }) {
  if (!data.length) return <div className="muted">no history</div>;
  const w = 276, h = 48, max = Math.max(1, ...data.map((d) => d.mentions));
  const pts = data.map((d, i) => `${(i / Math.max(1, data.length - 1)) * w},${h - (d.mentions / max) * (h - 2)}`).join(" ");
  return (
    <svg width={w} height={h} style={{ display: "block", marginTop: 6 }}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" />
      <text x={w} y={10} textAnchor="end" fontSize="10" fill="currentColor">{max}</text>
    </svg>
  );
}
