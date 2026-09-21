import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// Ground-truth events from the primary feeds (USGS, GDACS): not news, not
// counted in attention, drawn as their own layer. props carries the
// adapter-specific fields; kind/title/alert/mag are the ones the map uses.
// Quakes are windowed by age of first sighting; GDACS alerts by their last
// modification within 30 days, because a drought alert is added once and
// lives for months (and GDACS's iscurrent flag is false on live Orange
// droughts, measured 2026-09-20, so it cannot be the filter).
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const hours = clampInt(q.get("hours"), 72, 1, 720);
  const data = await rows(
    `SELECT e.id, s.kind AS source, e.external_id, e.added_at, e.occurred_on, e.geo_name, e.country,
            ST_Y(e.geom)::float AS lat, ST_X(e.geom)::float AS lon, e.url, e.props
       FROM event e JOIN source s ON s.id = e.source_id
      WHERE NOT s.attention
        AND coalesce(e.props->>'alert', '') <> 'Green'
        AND CASE WHEN s.kind = 'gdacs' THEN (e.props->>'modified')::timestamptz > now() - interval '30 days'
                 ELSE e.added_at > now() - make_interval(hours => $1) END
      ORDER BY e.added_at DESC
      LIMIT 500`,
    [hours],
  );
  return NextResponse.json({ hours, events: data });
}
