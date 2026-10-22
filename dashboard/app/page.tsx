import { Card, Shell, Stat } from "@/components/Shell";
import { cents, count, pct, time, tone, usd } from "@/lib/format";
import { isSettled, loadPicks } from "@/lib/picks";
import { strategyById } from "@/lib/strategies";
import { todayView } from "@/lib/views";

export const revalidate = 120;

/** "NFL|26OCT22NECHI" -> "NFL · NECHI": Kalshi runs the two team codes together */
function gameTitle(game: string) {
  const [league, code] = game.split("|");
  return `${league} · ${code.slice(7)}`;
}

export default async function TodayPage() {
  const { picks, error, fetchedAt } = await loadPicks();
  const byId = strategyById();
  const { todays, games, pending, settledToday, profitToday } = todayView(picks, fetchedAt);
  const allSettled = picks.filter(isSettled);
  const allCost = allSettled.reduce((a, p) => a + p.cost, 0);
  const allProfit = allSettled.reduce((a, p) => a + (p.profit ?? 0), 0);

  return (
    <Shell active="/">
      <div>
        <h1 className="text-2xl font-semibold">Today</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          Paper picks: what each strategy would buy 10–20 minutes before the start, at the live Kalshi price, one contract each.
          Nothing here is a real bet yet.
        </p>
      </div>

      {error && (
        <p className="rounded-md border border-down/40 px-3 py-2 text-sm text-down">Couldn’t load picks: {error}</p>
      )}

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Picks today" value={count(todays.length)} note={`${games.size} game${games.size === 1 ? "" : "s"}`} />
        <Stat label="Waiting on results" value={count(pending)} />
        <Stat label="Settled today" value={usd(settledToday ? profitToday : null)} toneClass={tone(profitToday)}
          note={`${settledToday} settled`} />
        <Stat label="Paper P&L, all time" value={usd(allSettled.length ? allProfit : null)} toneClass={tone(allProfit)}
          note={allCost ? `${pct(allProfit / allCost, 1)} on $${allCost.toFixed(0)} staked` : "no settled picks yet"} />
      </section>

      {games.size === 0 ? (
        <Card title="No picks yet today">
          <p className="text-sm text-muted">
            The scanner checks every 10 minutes and logs picks about 20 minutes before each game starts.
            NFL, NBA, NHL and college football games tonight will show up here as they approach.
          </p>
        </Card>
      ) : (
        [...games.entries()].map(([game, rows]) => (
          <Card
            key={game}
            title={gameTitle(game)}
            note={`Starts ${time(rows[0].gameStart)} CT · ${rows.length} pick${rows.length === 1 ? "" : "s"}`}
          >
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-muted">
                    <th className="py-1.5 pr-3 font-normal">Market</th>
                    <th className="py-1.5 pr-3 font-normal">Strategy</th>
                    <th className="py-1.5 pr-3 text-right font-normal">Buy</th>
                    <th className="py-1.5 pr-3 text-right font-normal">Available</th>
                    <th className="py-1.5 text-right font-normal">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((p) => (
                    <tr key={`${p.ticker}|${p.strategy}`} className="border-b border-line/60 last:border-0">
                      <td className="py-2 pr-3">{p.title}</td>
                      <td className="py-2 pr-3 text-muted">{byId.get(p.strategy)?.name ?? p.strategy}</td>
                      <td className="num py-2 pr-3 text-right">{p.side} {cents(p.price)}</td>
                      <td className="num py-2 pr-3 text-right text-muted">{count(p.askSize)}</td>
                      <td className={`num py-2 text-right ${tone(p.profit)}`}>
                        {p.result === "void" ? "void" : p.result ? usd(p.profit) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))
      )}
    </Shell>
  );
}
