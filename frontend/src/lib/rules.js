// Notification rule types, shared by the Rules page (form + list) and the
// Dashboard (per-stock alert summary). Values match the backend's
// NotificationEventType enum.
export const EVENT_TYPES = [
  { value: 'price_change_range', label: 'Price change outside band (%)', short: 'Outside band', needsBand: true },
  { value: 'week_low', label: 'Hits new weekly low', short: 'New weekly low' },
  { value: 'week_high', label: 'Hits new weekly high', short: 'New weekly high' },
  { value: 'month_low', label: 'Hits new monthly low', short: 'New monthly low' },
  { value: 'month_high', label: 'Hits new monthly high', short: 'New monthly high' },
  { value: 'quarter_low', label: 'Hits new quarterly low', short: 'New quarterly low' },
  { value: 'quarter_high', label: 'Hits new quarterly high', short: 'New quarterly high' },
  { value: 'year_low', label: 'Hits new yearly low', short: 'New yearly low' },
  { value: 'year_high', label: 'Hits new yearly high', short: 'New yearly high' },
];

const formatPct = (n) => `${n > 0 ? '+' : ''}${n}%`;

// Short, plain-language description of when a rule sends an email,
// e.g. "Moves below −3% or above +3%" or "New monthly high".
export function describeRule(rule) {
  if (rule.event_type === 'price_change_range') {
    if (rule.pct_low == null || rule.pct_high == null) return 'Outside band (not set)';
    return `Moves below ${formatPct(rule.pct_low)} or above ${formatPct(rule.pct_high)}`;
  }
  const type = EVENT_TYPES.find((e) => e.value === rule.event_type);
  return type?.short ?? rule.event_type;
}
