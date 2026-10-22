import { Card, Shell, VerdictPill } from "@/components/Shell";
import { count, pct, tone } from "@/lib/format";
import { legCorrelations, playerFactors, strategies, VERDICT_ORDER } from "@/lib/strategies";

export const metadata = { title: "Strategies · Hedj" };

const GROUP = { Bet: "Ready to paper trade", Watch: "Watch only", Stop: "Edge faded this season", Avoid: "No edge after fees" };

export default function BoardPage() {
  const all = strategies().sort((a, b) => VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict] || (b.roiAll ?? 0) - (a.roiAll ?? 0));
  const groups = (["Bet", "Watch", "Stop", "Avoid"] as const).map((v) => [v, all.filter((s) => s.verdict === v)] as const);

  return (
    <Shell active="/board">
      <div>
        <h1 className="text-2xl font-semibold">Strategies</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          Every rule run against every settled Kalshi market at the price 15 minutes before the game, after taker fees.
          Bet = profitable in both seasons on 150+ bets each and 5%+ overall.
        </p>
      </div>

      <Card title="Scorecard" note="Return = profit ÷ money spent, $1 a contract. Last month shows whether the edge is still there.">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-right text-xs text-muted">
                <th className="py-1.5 pr-3 text-left font-normal">Verdict</th>
                <th className="py-1.5 pr-3 text-left font-normal">Strategy</th>
                <th className="py-1.5 pr-3 font-normal">2025–26</th>
                <th className="py-1.5 pr-3 font-normal">2026–27</th>
                <th className="py-1.5 font-normal">Latest month</th>
              </tr>
            </thead>
            {groups.map(([verdict, rows]) => rows.length > 0 && (
              <tbody key={verdict}>
                <tr><td colSpan={5} className="pb-1 pt-4 text-xs text-muted">{GROUP[verdict]}</td></tr>
                {rows.map((s) => {
                  const last = s.months[s.months.length - 1];
                  return (
                    <tr key={s.id} className="border-b border-line/60 text-right align-top last:border-0">
                      <td className="py-2 pr-3 text-left"><VerdictPill verdict={s.verdict} /></td>
                      <td className="min-w-56 py-2 pr-3 text-left">
                        <div>{s.league} · {s.name}</div>
                        <div className="text-xs text-muted">{s.rule}</div>
                      </td>
                      <td className="num py-2 pr-3">
                        <div className={tone(s.roi2025)}>{pct(s.roi2025)}</div>
                        <div className="text-xs text-muted">{count(s.bets2025)} bets</div>
                      </td>
                      <td className="num py-2 pr-3">
                        <div className={tone(s.roi2026)}>{pct(s.roi2026)}</div>
                        <div className="text-xs text-muted">{count(s.bets2026)} bets</div>
                      </td>
                      <td className="num py-2">
                        <div className={tone(last?.roi ?? null)}>{pct(last?.roi ?? null)}</div>
                        <div className="text-xs text-muted">{last?.month} · {count(last?.bets ?? 0)}</div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </table>
        </div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Same-game leg pairs"
          note="How often both legs hit vs. what their prices imply if unrelated. Kalshi already charges ~1.2× for QB + receiver stacks, so this doesn't make combos +EV.">
          <table className="w-full text-sm">
            <tbody>
              {[...legCorrelations].sort((a, b) => b.ratio - a.ratio).map((c) => (
                <tr key={c.pair} className="border-b border-line/60 last:border-0">
                  <td className="py-1.5 pr-3">{c.pair}</td>
                  <td className={`num py-1.5 text-right ${c.ratio >= 1.15 ? "text-up" : c.ratio < 0.95 ? "text-down" : "text-muted"}`}>
                    {c.ratio.toFixed(2)}×
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Player factors that held up" note="Points more often the favored end beat its price, by season.">
          <table className="w-full text-sm">
            <tbody>
              {playerFactors.map((f) => (
                <tr key={f.factor} className="border-b border-line/60 align-top last:border-0">
                  <td className="py-1.5 pr-3">
                    <div>{f.factor}</div>
                    <div className="text-xs text-muted">{f.meaning}</div>
                  </td>
                  <td className="num py-1.5 text-right text-up">
                    {(f.edge_2025 * 100).toFixed(1)} / {(f.edge_2026 * 100).toFixed(1)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </Shell>
  );
}
