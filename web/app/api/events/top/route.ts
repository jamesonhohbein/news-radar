import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// Top events by first-window source count, one per article URL: a single
// article yields several GDELT events (each actor pair and place), and the
// map wants stories, not rows.
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const hours = clampInt(q.get("hours"), 24, 1, 168);
  const limit = clampInt(q.get("limit"), 300, 1, 2000);
  const data = await rows(
    `SELECT e.id, e.external_id, e.added_at, e.cameo_root, e.quad_class, e.goldstein, e.tone,
            e.actor1_name, e.actor2_name, e.geo_name, e.country, e.adm1,
            ST_Y(e.geom)::float AS lat, ST_X(e.geom)::float AS lon,
            e.num_mentions, e.num_sources, e.num_articles, e.url,
            s.title, s.site
       FROM (
         SELECT DISTINCT ON (url) *
           FROM event
          WHERE added_at > make_interval(hours => $1) * -1 + now() AND url IS NOT NULL
          ORDER BY url, num_sources DESC, num_mentions DESC
       ) e
       LEFT JOIN story s ON s.url = e.url AND s.status = 'ok'
      ORDER BY e.num_sources DESC, e.num_mentions DESC
      LIMIT $2`,
    [hours, limit],
  );
  return NextResponse.json({ hours, events: data });
}
