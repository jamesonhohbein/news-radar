// Read-only pool as the `reader` role. Credentials come from the repo-root
// .env (the systemd unit passes it as EnvironmentFile); in dev, from
// web/.env.local or the shell.
import { Pool } from "pg";

declare global {
  // eslint-disable-next-line no-var
  var __newsradarPool: Pool | undefined;
}

export function pool(): Pool {
  if (!global.__newsradarPool) {
    global.__newsradarPool = new Pool({
      host: process.env.POSTGRES_HOST ?? "127.0.0.1",
      port: Number(process.env.POSTGRES_PORT ?? 5443),
      database: process.env.POSTGRES_DB ?? "newsradar",
      user: "reader",
      password: process.env.READER_PASSWORD,
      max: 4,
    });
  }
  return global.__newsradarPool;
}

export async function rows<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  const r = await pool().query(sql, params);
  return r.rows as T[];
}

export function clampInt(v: string | null, def: number, min: number, max: number): number {
  const n = Number(v ?? def);
  return Number.isFinite(n) ? Math.min(max, Math.max(min, Math.trunc(n))) : def;
}
