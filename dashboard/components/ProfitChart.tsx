"use client";

import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export type ProfitPoint = { label: string; profit: number; bets: number };

const money = (v: number) => `${v < 0 ? "−" : ""}$${Math.abs(v).toFixed(2)}`;

export function ProfitChart({ points }: { points: ProfitPoint[] }) {
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <XAxis dataKey="label" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false}
            axisLine={{ stroke: "var(--line)" }} minTickGap={24} />
          <YAxis tickFormatter={money} tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false}
            axisLine={false} width={56} />
          <ReferenceLine y={0} stroke="var(--line)" />
          <Tooltip
            contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 6, fontSize: 12 }}
            labelStyle={{ color: "var(--muted)" }}
            formatter={(v) => [money(Number(v)), "Running profit"]}
          />
          <Line type="linear" dataKey="profit" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
