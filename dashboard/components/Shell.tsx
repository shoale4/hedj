import Link from "next/link";
import { logout } from "@/app/login/actions";

const TABS = [
  { href: "/", label: "Today" },
  { href: "/results", label: "Results" },
  { href: "/board", label: "Strategies" },
];

export function Shell({ active, children }: { active: string; children: React.ReactNode }) {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 px-4 pb-16 pt-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-4">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-xs uppercase tracking-[0.2em] text-muted">Hedj</span>
          <nav className="flex gap-1" aria-label="Sections">
            {TABS.map((t) => (
              <Link
                key={t.href}
                href={t.href}
                aria-current={active === t.href ? "page" : undefined}
                className={`rounded-md px-2.5 py-1 text-sm transition-colors ${
                  active === t.href ? "bg-panel text-fg" : "text-muted hover:text-fg"
                }`}
              >
                {t.label}
              </Link>
            ))}
          </nav>
        </div>
        <form action={logout}>
          <button type="submit" className="text-xs text-muted hover:text-fg">Sign out</button>
        </form>
      </header>
      {children}
    </div>
  );
}

export function Card({ title, note, children, id }: { title: string; note?: string; children: React.ReactNode; id?: string }) {
  return (
    <section id={id} className="flex min-w-0 flex-col gap-3 rounded-lg border border-line bg-panel p-4 sm:p-5">
      <div>
        <h2 className="text-base font-medium">{title}</h2>
        {note && <p className="mt-1 max-w-prose text-sm text-muted">{note}</p>}
      </div>
      {children}
    </section>
  );
}

export function Stat({ label, value, note, toneClass = "" }: { label: string; value: string; note?: string; toneClass?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border border-line bg-panel p-4">
      <span className="text-xs text-muted">{label}</span>
      <span className={`num text-2xl ${toneClass}`}>{value}</span>
      {note && <span className="text-xs text-muted">{note}</span>}
    </div>
  );
}

const VERDICT_STYLE: Record<string, string> = {
  Bet: "border-up/50 text-up",
  Watch: "border-line text-muted",
  Stop: "border-down/50 text-down",
  Avoid: "border-down/30 text-down/80",
};

export function VerdictPill({ verdict }: { verdict: string }) {
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${VERDICT_STYLE[verdict] ?? ""}`}>
      {verdict}
    </span>
  );
}
