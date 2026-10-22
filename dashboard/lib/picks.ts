import { parseCsv } from "./csv";

// paper/picks.csv is committed by the paper-trade GitHub Action every 10 minutes
const PICKS_URL = process.env.PICKS_URL ?? "https://raw.githubusercontent.com/shoale4/hedj/main/paper/picks.csv";

export const TIME_ZONE = "America/Chicago";

export type Pick = {
  loggedAt: Date;
  strategy: string;
  ticker: string;
  title: string;
  gameStart: Date;
  leadMin: number;
  side: "YES" | "NO";
  yesBid: number;
  yesAsk: number;
  price: number;
  fee: number;
  cost: number;
  askSize: number;
  openInterest: number;
  result: "" | "yes" | "no" | "void";
  won: boolean | null;
  profit: number | null;
  settledAt: Date | null;
};

const num = (v: string) => (v === "" ? 0 : Number(v));

export async function loadPicks(): Promise<{ picks: Pick[]; fetchedAt: Date; error?: string }> {
  try {
    const res = await fetch(PICKS_URL, { next: { revalidate: 120 } });
    if (!res.ok) throw new Error(`picks.csv returned ${res.status}`);
    const rows = parseCsv(await res.text());
    const picks = rows.map((r) => ({
      loggedAt: new Date(r.logged_at),
      strategy: r.strategy,
      ticker: r.ticker,
      title: r.title,
      gameStart: new Date(r.game_start),
      leadMin: num(r.lead_min),
      side: r.side as Pick["side"],
      yesBid: num(r.yes_bid),
      yesAsk: num(r.yes_ask),
      price: num(r.price),
      fee: num(r.fee),
      cost: num(r.cost),
      askSize: num(r.ask_size),
      openInterest: num(r.open_interest),
      result: r.result as Pick["result"],
      won: r.won === "" ? null : r.won === "1",
      profit: r.profit === "" ? null : Number(r.profit),
      settledAt: r.settled_at ? new Date(r.settled_at) : null,
    }));
    return { picks, fetchedAt: new Date() };
  } catch (e) {
    return { picks: [], fetchedAt: new Date(), error: e instanceof Error ? e.message : String(e) };
  }
}

export const isSettled = (p: Pick) => p.result === "yes" || p.result === "no";

/** "2026-10-22" for a date in Chicago time */
export function dayKey(d: Date): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

const LEAGUES: [string, string][] = [["KXNCAAF", "College football"], ["KXNFL", "NFL"], ["KXNBA", "NBA"], ["KXNHL", "NHL"]];

export function leagueOf(ticker: string): string {
  const series = ticker.split("-")[0];
  return LEAGUES.find(([prefix]) => series.startsWith(prefix))?.[1] ?? series;
}

/** The game a market belongs to across all its market types, e.g. "NFL|26OCT22NECHI" */
export function gameOf(ticker: string): string {
  return `${leagueOf(ticker)}|${ticker.split("-")[1] ?? ""}`;
}
