import { parseCsv } from "./csv";

// paper/parlays.csv is written by scripts/parlay_builder.py and settled by the paper-trade Action
const PARLAYS_URL =
  process.env.PARLAYS_URL ?? "https://raw.githubusercontent.com/shoale4/hedj/main/paper/parlays.csv";

export type Leg = {
  ticker: string;
  title: string;
  gameStart: Date;
  ask: number;
  fair: number;
  why: string;
  result: "" | "yes" | "no";
};

export const MONTHLY_BUDGET = 100;

export type Parlay = {
  id: string;
  kind: "regular" | "big swing";
  builtAt: Date;
  status: "open" | "won" | "lost";
  fair: number;
  estPrice: number;
  maxPrice: number;
  stake: number;
  legs: Leg[];
  /** Profit if bought at the max price; null while open */
  profit: number | null;
};

export async function loadParlays(): Promise<{ parlays: Parlay[]; error?: string }> {
  try {
    const res = await fetch(PARLAYS_URL, { next: { revalidate: 120 } });
    if (res.status === 404) return { parlays: [] };
    if (!res.ok) throw new Error(`parlays.csv returned ${res.status}`);
    const byId = new Map<string, Parlay>();
    for (const r of parseCsv(await res.text())) {
      let p = byId.get(r.parlay_id);
      if (!p) {
        const stake = Number(r.stake);
        const maxPrice = Number(r.max_price);
        const status = r.status as Parlay["status"];
        p = {
          id: r.parlay_id,
          kind: r.kind === "big swing" ? "big swing" : "regular",
          builtAt: new Date(r.built_at),
          status,
          fair: Number(r.parlay_fair),
          estPrice: Number(r.est_price),
          maxPrice,
          stake,
          legs: [],
          profit: status === "won" ? stake / maxPrice - stake : status === "lost" ? -stake : null,
        };
        byId.set(p.id, p);
      }
      p.legs.push({
        ticker: r.ticker,
        title: r.title,
        gameStart: new Date(r.game_start),
        ask: Number(r.leg_ask),
        fair: Number(r.leg_fair),
        why: r.why,
        result: r.leg_result as Leg["result"],
      });
    }
    const parlays = [...byId.values()].sort((a, b) => +b.builtAt - +a.builtAt || a.id.localeCompare(b.id));
    return { parlays };
  } catch (e) {
    return { parlays: [], error: e instanceof Error ? e.message : String(e) };
  }
}
