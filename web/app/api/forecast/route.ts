import { NextRequest, NextResponse } from "next/server";
import { clampInt, rows } from "@/lib/db";

export const dynamic = "force-dynamic";

// VIEWS predicted state-based deaths per country (R36), from the newest
// loaded run, summed over the next `months` forecast months starting with
// the current one. Every run is kept; "newest" is the last one fetched.
export async function GET(req: NextRequest) {
  const months = clampInt(req.nextUrl.searchParams.get("months"), 1, 1, 36);
  const data = await rows<{ run: string; country: string; fatalities: number; p_any: number | null }>(
    `WITH latest AS (
       SELECT f.file FROM fetch_log f JOIN source s ON s.id = f.source_id AND s.name = 'views-forecast'
        WHERE f.file LIKE 'fatalities%' ORDER BY f.fetched_at DESC LIMIT 1
     )
     SELECT c.run, c.country, sum(c.fatalities)::float AS fatalities, max(c.p_any)::float AS p_any
       FROM conflict_forecast c JOIN latest l ON l.file = c.run
      WHERE c.month >= date_trunc('month', now())::date
        AND c.month < (date_trunc('month', now()) + make_interval(months => $1))::date
      GROUP BY c.run, c.country
      ORDER BY fatalities DESC`,
    [months],
  );
  return NextResponse.json({ months, run: data[0]?.run ?? null, countries: data });
}
