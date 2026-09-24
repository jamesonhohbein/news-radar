import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// Top stories by distinct outlets covering them in the window (R20), with
// syndicated reprints collapsed by normalized headline (R21). Ranking lives in
// the top_stories() SQL function so it is tested beside the schema; each story
// is drawn at its most-mentioned event. The window reads the 72 h mention
// buffer, so it caps there.
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const hours = clampInt(q.get("hours"), 24, 1, 72);
  const limit = clampInt(q.get("limit"), 300, 1, 2000);
  const data = await rows(
    `SELECT e.id, e.external_id, e.added_at, e.cameo_root, e.quad_class, e.goldstein, e.tone,
            e.actor1_name, e.actor2_name, e.geo_name, e.country, e.adm1,
            ST_Y(e.geom)::float AS lat, ST_X(e.geom)::float AS lon,
            e.num_mentions, e.num_sources, e.num_articles, e.url,
            s.title, s.site,
            t.outlets, t.outlets_1h, t.mentions AS window_mentions, t.sites
       FROM top_stories($1, $2) t
       JOIN event e ON e.id = t.rep
       LEFT JOIN story s ON s.url = e.url AND s.status = 'ok'
      ORDER BY t.outlets DESC, t.outlets_1h DESC`,
    [hours, limit],
  );
  return NextResponse.json({ hours, events: data });
}
