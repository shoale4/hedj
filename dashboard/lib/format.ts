import { TIME_ZONE } from "./picks";

export const pct = (v: number | null, digits = 0) => {
  if (v === null || Number.isNaN(v)) return "—";
  const shown = (v * 100).toFixed(digits);
  if (Number(shown) === 0) return `${(0).toFixed(digits)}%`;
  return `${v > 0 ? "+" : ""}${shown}%`;
};

export const cents = (v: number) => `${Math.round(v * 100)}¢`;

export const usd = (v: number | null) =>
  v === null ? "—" : `${v < 0 ? "−" : v > 0 ? "+" : ""}$${Math.abs(v).toFixed(2)}`;

export const count = (v: number) => v.toLocaleString("en-US");

export const time = (d: Date) =>
  d.toLocaleTimeString("en-US", { timeZone: TIME_ZONE, hour: "numeric", minute: "2-digit" });

export const dayTime = (d: Date) =>
  d.toLocaleString("en-US", { timeZone: TIME_ZONE, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export const tone = (v: number | null) => (v === null || v === 0 ? "" : v > 0 ? "text-up" : "text-down");
