import monthly from "@/data/board/strategies.json";
import correlations from "@/data/board/correlations.json";
import factors from "@/data/board/factors.json";

// Strategy board data comes from scripts/build_dashboard_data.py

export type Verdict = "Bet" | "Watch" | "Stop" | "Avoid";

export type Strategy = {
  id: string;
  league: string;
  name: string;
  rule: string;
  side: string;
  verdict: Verdict;
  roi2025: number | null;
  bets2025: number;
  roi2026: number | null;
  bets2026: number;
  roiAll: number | null;
  months: { month: string; bets: number; roi: number | null }[];
};

type MonthRow = { strategy: string; league: string; name: string; rule: string; side: string;
  month: string; season: string; bets: number; cost: number; profit: number };

const roi = (profit: number, cost: number) => (cost > 0 ? profit / cost : null);

// Same rules as the strategy board artifact
function verdictOf(roiA: number | null, betsA: number, roiB: number | null, betsB: number, all: number | null): Verdict {
  if (roiA !== null && roiA > 0 && betsA >= 150 && roiB !== null && roiB <= 0 && betsB >= 500) return "Stop";
  if (all === null || all <= 0.02) return "Avoid";
  if (roiA !== null && roiB !== null && roiA > 0 && roiB > 0 && betsA >= 150 && betsB >= 150 && all >= 0.05) return "Bet";
  return "Watch";
}

export function strategies(): Strategy[] {
  const by = new Map<string, MonthRow[]>();
  for (const r of monthly as MonthRow[]) by.set(r.strategy, [...(by.get(r.strategy) ?? []), r]);
  return [...by.entries()].map(([id, rows]) => {
    const sum = (season?: string) => rows.filter((r) => !season || r.season === season)
      .reduce((a, r) => ({ bets: a.bets + r.bets, cost: a.cost + r.cost, profit: a.profit + r.profit }), { bets: 0, cost: 0, profit: 0 });
    const a = sum("2025-26"), b = sum("2026-27"), all = sum();
    const roiA = roi(a.profit, a.cost), roiB = roi(b.profit, b.cost), roiAll = roi(all.profit, all.cost);
    const first = rows[0];
    return {
      id, league: first.league, name: first.name, rule: first.rule, side: first.side,
      verdict: verdictOf(roiA, a.bets, roiB, b.bets, roiAll),
      roi2025: roiA, bets2025: a.bets, roi2026: roiB, bets2026: b.bets, roiAll,
      months: [...rows].sort((x, y) => x.month.localeCompare(y.month))
        .map((r) => ({ month: r.month, bets: r.bets, roi: roi(r.profit, r.cost) })),
    };
  });
}

export function strategyById(): Map<string, Strategy> {
  return new Map(strategies().map((s) => [s.id, s]));
}

export type Correlation = { pair: string; pairs: number; both_hit: number; priced: number; ratio: number };
export type Factor = { factor: string; meaning: string; edge_2025: number; edge_2026: number };

export const legCorrelations = correlations as Correlation[];
export const playerFactors = factors as Factor[];

export const VERDICT_ORDER: Record<Verdict, number> = { Bet: 0, Watch: 1, Stop: 2, Avoid: 3 };
