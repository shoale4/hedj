import { Card, Shell, Stat } from "@/components/Shell";
import { dayTime, pct, tone, usd } from "@/lib/format";
import { dayKey } from "@/lib/picks";
import { loadParlays, MONTHLY_BUDGET, type Leg } from "@/lib/parlays";

export const revalidate = 120;

const american = (p: number) => `+${Math.round((1 / p - 1) * 100)}`;

function LegMark({ leg }: { leg: Leg }) {
  if (leg.result === "yes") return <span className="text-up">✓</span>;
  if (leg.result === "no") return <span className="text-down">✗</span>;
  return <span className="text-muted">·</span>;
}

export default async function ParlaysPage() {
  const { parlays, error } = await loadParlays();
  const settled = parlays.filter((p) => p.profit !== null);
  const staked = settled.reduce((a, p) => a + p.stake, 0);
  const profit = settled.reduce((a, p) => a + (p.profit ?? 0), 0);
  const won = settled.filter((p) => p.status === "won").length;
  const open = parlays.filter((p) => p.status === "open");
  const month = dayKey(new Date()).slice(0, 7);
  const spentThisMonth = parlays.filter((p) => dayKey(p.builtAt).startsWith(month)).reduce((a, p) => a + p.stake, 0);

  return (
    <Shell active="/parlays">
      <div>
        <h1 className="text-2xl font-semibold">Parlays</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          Three 3–4 leg NFL parlays and one longer big swing a week, built from legs Kalshi prices at or below the sportsbooks’ fair odds,
          ranked by recent form and matchup. Only buy at the max price or less. Results assume you paid the max price.
          Budget rule: about $20 a week. After four weeks in profit, raise it 25%; if a month loses the full budget, pause and review.
        </p>
      </div>

      {error && <p className="rounded-md border border-down/40 px-3 py-2 text-sm text-down">Couldn’t load parlays: {error}</p>}

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Open" value={String(open.length)} note={`$${open.reduce((a, p) => a + p.stake, 0).toFixed(0)} staked`} />
        <Stat label="Record" value={`${won}–${settled.length - won}`} note="won–lost" />
        <Stat label="Profit" value={usd(settled.length ? profit : null)} toneClass={tone(profit)}
          note={staked ? `${pct(profit / staked, 0)} on $${staked.toFixed(0)}` : "nothing settled yet"} />
        <Stat label="This month" value={`$${spentThisMonth.toFixed(0)}`} note={`of $${MONTHLY_BUDGET} budget`}
          toneClass={spentThisMonth > MONTHLY_BUDGET ? "text-down" : ""} />
      </section>

      {parlays.length === 0 && (
        <Card title="No parlays yet">
          <p className="text-sm text-muted">The first card is built before the weekend’s games.</p>
        </Card>
      )}

      {parlays.map((p) => (
        <Card
          key={p.id}
          title={`${p.kind === "big swing" ? "Big swing" : "Parlay"} · ${p.legs.length} legs · ${american(p.maxPrice)} or better`}
          note={`Built ${dayTime(p.builtAt)} · fair ${(p.fair * 100).toFixed(1)}% · buy at ${(p.maxPrice * 100).toFixed(1)}¢ or less · $${p.stake.toFixed(0)}`}
        >
          <ul className="flex flex-col gap-2.5">
            {p.legs.map((l) => (
              <li key={l.ticker} className="flex gap-2.5 text-sm">
                <LegMark leg={l} />
                <div className="min-w-0">
                  <div>{l.title}</div>
                  <div className="text-xs text-muted">{dayTime(l.gameStart)} · {l.why}</div>
                </div>
              </li>
            ))}
          </ul>
          <div className={`num text-sm ${p.status === "won" ? "text-up" : p.status === "lost" ? "text-down" : "text-muted"}`}>
            {p.status === "open" ? "Open" : `${p.status === "won" ? "Won" : "Lost"} ${usd(p.profit)}`}
          </div>
        </Card>
      ))}
    </Shell>
  );
}
