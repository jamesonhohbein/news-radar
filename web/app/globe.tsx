"use client";

import * as maplibregl from "maplibre-gl";
import type { Map as MLMap, MapLayerMouseEvent } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";
import { CAMEO_ROOT, QUAD_CLASS } from "@/lib/cameo";

const STYLE = process.env.NEXT_PUBLIC_MAP_STYLE ?? "https://tiles.openfreemap.org/styles/positron";
const HOURS = 24;
const MIN_MENTIONS = 50;
const PRIMARY_HOURS = 72;
// Ingest lands every 15 min and rollups hourly; 5 min keeps an open tab
// within one ingest of the database without hammering it.
const REFRESH_MS = 5 * 60 * 1000;

type Region = { region: string; name: string | null; z: number; mentions: number; expected: number; peak_z: number | null };
type Ev = {
  id: number; added_at: string; cameo_root: string | null; quad_class: number | null;
  actor1_name: string | null; actor2_name: string | null; geo_name: string | null; country: string;
  lat: number; lon: number; num_mentions: number; num_sources: number; url: string | null;
  title: string | null; site: string | null;
  outlets: number; outlets_1h: number; window_mentions: number; sites: number;
};
type Daily = { day: string; mentions: number };
type Primary = {
  id: number; source: string; external_id: string; added_at: string; geo_name: string | null; country: string;
  lat: number; lon: number; url: string | null; props: { kind?: string; title?: string; alert?: string | null; mag?: number; population?: string | null; event?: string; severity?: string; expires?: string; category?: string; centre?: string; magnitude?: number; color?: string; color_prev?: string; synopsis?: string };
};

declare global { interface Window { __newsradar?: { ready: boolean; layers: string[]; regions: number; events: number; primary: number } } }

export default function Globe() {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const namesRef = useRef<Record<string, string>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [events, setEvents] = useState<Ev[]>([]);
  const [primary, setPrimary] = useState<Primary[]>([]);
  const [hover, setHover] = useState<{ fips: string; name: string } | null>(null);
  const [series, setSeries] = useState<Daily[]>([]);
  const [asOf, setAsOf] = useState<string | null>(null);

  useEffect(() => {
    if (!el.current || mapRef.current) return;
    // Flat (mercator) by decision 2026-09-20; the globe was tried and rejected.
    // renderWorldCopies off so a story is one dot, not three.
    const map = new maplibregl.Map({ container: el.current, style: STYLE, center: [10, 20], zoom: 1.5, minZoom: 1, renderWorldCopies: false, attributionControl: { compact: true } });
    mapRef.current = map;
    (window as unknown as { __map: MLMap }).__map = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    map.on("style.load", async () => {
      map.fitBounds([[-170, -58], [180, 78]], { padding: 8, duration: 0 });
      const countries = await fetch("/countries.geojson").then((r) => r.json());
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
      map.addSource("events", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "events", type: "circle", source: "events",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "outlets"], 2, 3, 150, 16],
          "circle-color": "#1d4ed8", "circle-opacity": 0.55, "circle-stroke-color": "#fff", "circle-stroke-width": 0.5,
        },
      });

      // Primary feeds: quakes (size by magnitude) and non-Green GDACS alerts,
      // in a colour of their own so ground truth is never confused with coverage.
      map.addSource("primary", { type: "geojson", data: EMPTY });
      map.addLayer({
        id: "primary", type: "circle", source: "primary",
        paint: {
          "circle-radius": ["case", ["==", ["get", "source"], "usgs"], ["interpolate", ["linear"], ["coalesce", ["get", "mag"], 4.5], 4.5, 4, 7.5, 16], 7],
          "circle-color": ["match", ["get", "source"], "usgs", "#b45309", "nws", "#0e7490", "tsunami", "#be123c", "volcano", "#c2410c", "#7c3aed"],
          "circle-opacity": 0.75, "circle-stroke-color": "#fff", "circle-stroke-width": 1,
        },
      });
      map.on("click", "primary", (e: MapLayerMouseEvent) => {
        const f = e.features?.[0]?.properties as (Omit<Primary, "props"> & { props: string }) | undefined;
        if (!f) return;
        const p: Primary = { ...f, props: JSON.parse(f.props) };
        new maplibregl.Popup({ closeButton: true }).setLngLat([p.lon, p.lat]).setHTML(primaryHtml(p)).addTo(map);
      });
      map.on("mouseenter", "primary", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "primary", () => { map.getCanvas().style.cursor = ""; });

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

      const counts = await load(map);
      window.__newsradar = { ready: true, layers: ["countries-fill", "events", "primary"], ...counts };
      timer = setInterval(() => { if (!document.hidden) load(map); }, REFRESH_MS);
    });

    // Everything but the country polygons is re-fetched and swapped in place,
    // so an open tab follows the database instead of freezing at page load.
    async function load(m: MLMap) {
      const [anomaly, top, prim, fresh] = await Promise.all([
        fetch("/api/anomaly?kind=country").then((r) => r.json()),
        fetch(`/api/events/top?hours=${HOURS}&limit=300`).then((r) => r.json()),
        fetch(`/api/events/primary?hours=${PRIMARY_HOURS}`).then((r) => r.json()),
        fetch("/api/freshness").then((r) => r.json()),
      ]);
      const regs: Region[] = anomaly.regions;
      const evs: Ev[] = top.events;
      const prims: Primary[] = prim.events;
      setRegions(regs);
      setEvents(evs);
      setPrimary(prims);
      setAsOf(fresh.fetched_at);
      for (const r of regs) if (r.name) namesRef.current[r.region] = r.name;
      // Clear first, or a region that dropped out of the window keeps its old tint.
      m.removeFeatureState({ source: "countries" });
      // Tint only regions with enough volume for z to mean something: a
      // microstate going from 1 mention to 25 scores z 24 and would glow.
      for (const r of regs) m.setFeatureState({ source: "countries", id: r.region }, { z: r.mentions >= MIN_MENTIONS ? r.z : 0, mentions: r.mentions });
      (m.getSource("events") as maplibregl.GeoJSONSource).setData({ type: "FeatureCollection", features: evs.map((e) => ({ type: "Feature", id: e.id, geometry: { type: "Point", coordinates: [e.lon, e.lat] }, properties: e })) });
      (m.getSource("primary") as maplibregl.GeoJSONSource).setData({ type: "FeatureCollection", features: prims.map((e) => ({ type: "Feature", id: e.id, geometry: { type: "Point", coordinates: [e.lon, e.lat] }, properties: { ...e, mag: e.props.mag ?? null, props: JSON.stringify(e.props) } })) });
      return { regions: regs.length, events: evs.length, primary: prims.length };
    }

    let timer: ReturnType<typeof setInterval> | undefined;
    const onVisible = () => { if (!document.hidden && map.getSource("events")) load(map); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); map.remove(); mapRef.current = null; };
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
        <div className="muted">Last {HOURS} h. Tint: share of world mentions vs the region&apos;s usual share over 30 days, as a z-score, regions with {MIN_MENTIONS}+ mentions. Dots: top {events.length} stories by distinct outlets covering them in that window, reprints of one headline counted as one story.</div>
        <div className="legend"><i /> z 1 → 5+ <b /> story <b style={{ background: "#b45309" }} /> quake M4.5+ <b style={{ background: "#7c3aed" }} /> GDACS alert <b style={{ background: "#0e7490" }} /> NWS severe <b style={{ background: "#be123c" }} /> tsunami <b style={{ background: "#c2410c" }} /> volcano</div>
        <div className="muted">{primary.length} primary events, last {PRIMARY_HOURS} h.</div>
        <div className="muted" data-testid="as-of">{asOf ? `Coverage as of ${new Date(asOf).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : "Loading…"} · refreshes every 5 min</div>
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

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

function firstSymbolLayer(map: MLMap): string | undefined {
  return map.getStyle().layers.find((l) => l.type === "symbol")?.id;
}

function popupHtml(p: Ev): string {
  const esc = (s: string | null | undefined) => (s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  const root = p.cameo_root ? CAMEO_ROOT[p.cameo_root] ?? p.cameo_root : "";
  const quad = p.quad_class ? QUAD_CLASS[p.quad_class] : "";
  const who = [p.actor1_name, p.actor2_name].filter(Boolean).map(esc).join(" → ") || "(unnamed actors)";
  const host = p.site || (p.url ? (() => { try { return new URL(p.url!).hostname; } catch { return p.url; } })() : "");
  const head = p.title ? `<strong>${esc(p.title)}</strong><br><span style="color:#666">${who}</span>` : `<strong>${who}</strong>`;
  return `<div>${head}<br>${esc(root)}${quad ? ` · ${esc(quad)}` : ""}<br>${esc(p.geo_name)}<br>` +
    `<span style="color:#666">${p.outlets} outlets in ${HOURS} h · ${p.outlets_1h} in the last hour${p.sites > 1 ? ` · ${p.sites} sites ran this headline` : ""}<br>first seen ${new Date(p.added_at).toUTCString().slice(5, 22)} UTC</span>` +
    (p.url ? `<br><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(host)}</a>` : "") + `</div>`;
}

function primaryHtml(p: Primary): string {
  const esc = (s: string | null | undefined) => (s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  const when = new Date(p.added_at).toUTCString().slice(5, 22) + " UTC";
  const line2 = p.source === "usgs" ? `M${p.props.mag} earthquake`
    : p.source === "volcano" ? `Volcano ${esc(p.props.color)} (was ${esc(p.props.color_prev)})${p.props.synopsis ? `<br>${esc(p.props.synopsis)}` : ""}`
    : p.source === "tsunami" ? `Tsunami ${esc(p.props.category)} · ${esc(p.props.centre)}${p.props.magnitude ? ` · M${p.props.magnitude}` : ""}`
    : p.source === "nws" ? `${esc(p.props.event)} · ${esc(p.props.severity)} · until ${p.props.expires ? new Date(p.props.expires).toUTCString().slice(5, 22) + " UTC" : "?"}`
    : `${esc(p.props.alert)} ${esc(p.props.kind)} alert${p.props.population ? ` · ${esc(p.props.population)}` : ""}`;
  return `<div><strong>${esc(p.props.title || p.geo_name)}</strong><br>${line2}<br><span style="color:#666">${esc(p.source)} · ${when}</span>` +
    (p.url ? `<br><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.source)} page</a>` : "") + `</div>`;
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
