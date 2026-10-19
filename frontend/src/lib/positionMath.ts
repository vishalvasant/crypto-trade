/** Live mark-to-market helpers matching the paper engine. */

export type OpenPos = {
  side?: string;
  size?: number;
  entry_price?: number;
  mark_price?: number;
  stop_price?: number;
  target_price?: number;
  trailing_stop?: number;
  margin?: number;
  leverage?: number;
  pnl?: number;
  entry_fee?: number;
  contract_value?: number;
  setup_name?: string;
  is_open?: boolean;
  opened_at?: string;
  updated_at?: string;
};

export function liveUnrealizedPnl(
  pos: OpenPos | null | undefined,
  mark: number,
  takerFeeRate = 0.0005
): number {
  if (!pos?.is_open || !(mark > 0)) return Number(pos?.pnl || 0);
  const size = Number(pos.size || 0);
  const entry = Number(pos.entry_price || 0);
  const contract = Number(pos.contract_value || 0.001);
  if (!(size > 0) || !(entry > 0)) return Number(pos.pnl || 0);
  const direction = ["BUY", "LONG"].includes(String(pos.side || "").toUpperCase()) ? 1 : -1;
  const gross = direction * (mark - entry) * size * contract;
  const entryFee =
    pos.entry_fee != null ? Number(pos.entry_fee) : entry * size * contract * takerFeeRate;
  const exitFee = size * contract * mark * takerFeeRate;
  return gross - entryFee - exitFee;
}

export function liveEquity(
  initialBalance: number,
  realizedPnl: number,
  unrealized: number
): number {
  return Number(initialBalance || 0) + Number(realizedPnl || 0) + Number(unrealized || 0);
}
