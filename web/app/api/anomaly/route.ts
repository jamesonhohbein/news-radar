import { NextRequest, NextResponse } from "next/server";
import { rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// Per region: its share of the world's mentions over the last 24 h against
// the distribution of its daily share over the prior 30 days, as a z-score.
// Share, not count, because counts carry the weekday cycle and GDELT's own
// volume drift: on a Sunday every country is "below baseline" in absolute
// terms while the interesting question is who got more of a smaller pie.
//
// The sd floor is the binomial one, sqrt(p/n) with n the 24 h world total,
// so a region that is normally silent and takes 1% of the world today still
// scores, without dividing by zero. `expected` is the mean share applied to
// today's total, for the panel. peak_z is the detector's hourly signal,
// carried along for reference; it is not the tint.
export async function GET(req: NextRequest) {
  const kind = req.nextUrl.searchParams.get("kind") === "adm1" ? "adm1" : "country";
  const data = await rows<{ region: string; name: string | null; mentions: number; expected: number; z: number; peak_z: number | null }>(
    `WITH tot AS (
       SELECT greatest(sum(mentions), 1)::float AS t
         FROM attention_hourly
        WHERE region_kind = $1 AND hour > now() - interval '24 hours'
     ), cur AS (
       SELECT h.region, sum(h.mentions)::int AS mentions, max(a.z)::float AS peak_z
         FROM attention_hourly h
         LEFT JOIN attention_anomaly a USING (region_kind, region, hour)
        WHERE h.region_kind = $1 AND h.hour > now() - interval '24 hours'
        GROUP BY h.region
     ), daily AS (
       SELECT region, mentions::float / greatest(sum(mentions) OVER (PARTITION BY day), 1) AS share
         FROM attention_daily
        WHERE region_kind = $1 AND day >= current_date - 30 AND day < current_date
     ), base AS (
       SELECT region, avg(share) AS mean, coalesce(stddev_pop(share), 0) AS sd FROM daily GROUP BY region
     )
     SELECT c.region, n.name, c.mentions,
            round((coalesce(b.mean, 0) * tot.t)::numeric)::int AS expected,
            ((c.mentions / tot.t - coalesce(b.mean, 0))
               / greatest(coalesce(b.sd, 0), sqrt(coalesce(b.mean, 0) / tot.t), 1.0 / tot.t))::float AS z,
            c.peak_z
       FROM cur c CROSS JOIN tot
       LEFT JOIN base b USING (region)
       LEFT JOIN region_name n ON n.region_kind = $1 AND n.region = c.region
      ORDER BY z DESC`,
    [kind],
  );
  return NextResponse.json({ hours: 24, kind, regions: data });
}
