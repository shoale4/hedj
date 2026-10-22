import type { ProfitPoint } from "@/components/ProfitChart";
import { dayTime } from "./format";
import { dayKey, gameOf, isSettled, type Pick } from "./picks";

/** Today's picks (Chicago time), plus anything from the last day still waiting on a result */
export function todayView(picks: Pick[], now: Date) {
  const today = dayKey(now);
  const dayAgo = new Date(now.getTime() - 24 * 3600e3);
  const todays = picks.filter((p) => dayKey(p.gameStart) === today || (!p.result && p.gameStart > dayAgo));
  const games = new Map<string, Pick[]>();
  for (const p of [...todays].sort((a, b) => +a.gameStart - +b.gameStart)) {
    const key = gameOf(p.ticker);
    games.set(key, [...(games.get(key) ?? []), p]);
  }
  const settledToday = todays.filter(isSettled);
  return {
    todays,
    games,
    pending: todays.filter((p) => !p.result).length,
    settledToday: settledToday.length,
    profitToday: settledToday.reduce((a, p) => a + (p.profit ?? 0), 0),
  };
}

/** Running profit over settled picks in game order */
export function profitSeries(picks: Pick[]): ProfitPoint[] {
  const settled = picks.filter(isSettled).sort((a, b) => +a.gameStart - +b.gameStart);
  const out: ProfitPoint[] = [];
  settled.reduce((running, p, i) => {
    const next = running + (p.profit ?? 0);
    out.push({ label: dayTime(p.gameStart), profit: Math.round(next * 100) / 100, bets: i + 1 });
    return next;
  }, 0);
  return out;
}
