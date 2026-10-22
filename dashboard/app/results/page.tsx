import { Card, Shell, Stat } from "@/components/Shell";
import { ProfitChart } from "@/components/ProfitChart";
import { count, pct, tone, usd } from "@/lib/format";
import { isSettled, loadPicks, type Pick } from "@/lib/picks";
import { strategyById } from "@/lib/strategies";
import { profitSeries } from "@/lib/views";

export const revalidate = 120;
export const metadata = { title: "Results · Hedj" };

const MIN_SAMPLE = 30;

type Row = {
  id: string; name: string; league: string; picks: number; settled: number; won: number;
  cost: number; profit: number; roi: number | null; margin: number | null; backtest: number | null; medianSize: number;
};

function summarize(id: string, picks: Pick[], backtest: number | null, name: string, league: string): Row {
  const done = picks.filter(isSettled);
  const cost = done.reduce((a, p) => a + p.cost, 0);
  const profit = done.reduce((a, p) => a + (p.profit ?? 0), 0);
  // 95% range on ROI from the spread of per-bet profits
  let margin: number | null = null;
  if (done.length > 1 && cost > 0) {
    const mean = profit / done.length;
    const sd = Math.sqrt(done.reduce((a, p) => a + ((p.profit ?? 0) - mean) ** 2, 0) / (done.length - 1));
    margin = (1.96 * sd * Math.sqrt(done.length)) / cost;
  }
  const sizes = picks.map((p) => p.askSize).sort((a, b) => a - b);
  return {
    id, name, league, picks: picks.length, settled: done.length, won: done.filter((p) => p.won).length,
    cost, profit, roi: cost ? profit / cost : null, margin, backtest, medianSize: sizes[Math.floor(sizes.length / 2)] ?? 0,
  };
}

function status(r: Row) {
  if (r.settled < MIN_SAMPLE) return { text: `Too early (${r.settled}/${MIN_SAMPLE})`, cls: "text-muted" };
  if (r.roi !== null && r.margin !== null && r.roi - r.margin > 0) return { text: "Beating zero", cls: "text-up" };
  if (r.roi !== null && r.roi > 0) return { text: "On track", cls: "text-up" };
  return { text: "Behind", cls: "text-down" };
}

export default async function ResultsPage() {
  const { picks, error } = await loadPicks();
  const byId = strategyById();
  const groups = new Map<string, Pick[]>();
  for (const p of picks) groups.set(p.strategy, [...(groups.get(p.strategy) ?? []), p]);
  const rows = [...groups.entries()]
    .map(([id, ps]) => {
      const s = byId.get(id);
      return summarize(id, ps, s?.roiAll ?? null, s?.name ?? id, s?.league ?? "");
    })
    .sort((a, b) => b.settled - a.settled);

  const points = profitSeries(picks);
  const total = summarize("all", picks, null, "All", "");

  return (
    <Shell active="/results">
      <div>
        <h1 className="text-2xl font-semibold">Results</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          How paper picks are doing against the backtest. A strategy needs {MIN_SAMPLE}+ settled picks before its
          number means much; the ± range shows how far luck alone could move it.
        </p>
      </div>

      {error && <p className="rounded-md border border-down/40 px-3 py-2 text-sm text-down">Couldn’t load picks: {error}</p>}

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Settled picks" value={count(total.settled)} note={`${count(total.picks)} logged`} />
        <Stat label="Won" value={total.settled ? pct(total.won / total.settled).replace("+", "") : "—"} />
        <Stat label="Profit" value={usd(total.settled ? total.profit : null)} toneClass={tone(total.profit)}
          note={`on $${total.cost.toFixed(2)} staked`} />
        <Stat label="Return" value={pct(total.roi, 1)} toneClass={tone(total.roi)}
          note={total.margin !== null ? `± ${(total.margin * 100).toFixed(0)}%` : undefined} />
      </section>

      <Card title="Running profit" note="Every settled paper pick at $1 a contract, in game order.">
        {points.length > 1 ? <ProfitChart points={points} /> : (
          <p className="text-sm text-muted">The chart starts once a few picks have settled.</p>
        )}
      </Card>

      <Card title="By strategy" note="Live paper results next to what the backtest made over 2025–27.">
        {rows.length === 0 ? <p className="text-sm text-muted">No picks logged yet.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-right text-xs text-muted">
                  <th className="py-1.5 pr-3 text-left font-normal">Strategy</th>
                  <th className="py-1.5 pr-3 font-normal">Settled</th>
                  <th className="py-1.5 pr-3 font-normal">Profit</th>
                  <th className="py-1.5 pr-3 font-normal">Return</th>
                  <th className="py-1.5 pr-3 font-normal">Backtest</th>
                  <th className="py-1.5 pr-3 font-normal">Typical size</th>
                  <th className="py-1.5 text-left font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const st = status(r);
                  return (
                    <tr key={r.id} className="border-b border-line/60 text-right last:border-0">
                      <td className="py-2 pr-3 text-left">
                        <span className="text-muted">{r.league} · </span>{r.name}
                      </td>
                      <td className="num py-2 pr-3">{count(r.settled)}<span className="text-muted">/{count(r.picks)}</span></td>
                      <td className={`num py-2 pr-3 ${tone(r.profit)}`}>{usd(r.settled ? r.profit : null)}</td>
                      <td className={`num py-2 pr-3 ${tone(r.roi)}`}>
                        {pct(r.roi)}{r.margin !== null && <span className="text-muted"> ±{(r.margin * 100).toFixed(0)}</span>}
                      </td>
                      <td className="num py-2 pr-3 text-muted">{pct(r.backtest)}</td>
                      <td className="num py-2 pr-3 text-muted">{count(r.medianSize)}</td>
                      <td className={`py-2 text-left ${st.cls}`}>{st.text}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </Shell>
  );
}
