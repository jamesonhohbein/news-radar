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
    `SELECT id, external_id, added_at, cameo_root, quad_class, goldstein, tone,
            actor1_name, actor2_name, geo_name, country, adm1,
            ST_Y(geom)::float AS lat, ST_X(geom)::float AS lon,
            num_mentions, num_sources, num_articles, url
       FROM (
         SELECT DISTINCT ON (url) *
           FROM event
          WHERE added_at > now() - make_interval(hours => $1) AND url IS NOT NULL
          ORDER BY url, num_sources DESC, num_mentions DESC
       ) e
      ORDER BY num_sources DESC, num_mentions DESC
      LIMIT $2`,
    [hours, limit],
  );
  return NextResponse.json({ hours, events: data });
}
