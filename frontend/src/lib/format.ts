export function fmt(n: number | null | undefined, d = 2): string {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function fmtCompact(n: number | null | undefined, d = 1): string {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(2)}K`;
  return fmt(v, d);
}

export function fmtPct(n: number | null | undefined, d = 2): string {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return `${v > 0 ? "+" : ""}${v.toFixed(d)}%`;
}

export function fmtTimeIST(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

export function sideClass(side?: string): string {
  const s = String(side || "").toUpperCase();
  if (s === "LONG" || s === "BUY" || s === "BULLISH") return "long";
  if (s === "SHORT" || s === "SELL" || s === "BEARISH") return "short";
  return "wait";
}
