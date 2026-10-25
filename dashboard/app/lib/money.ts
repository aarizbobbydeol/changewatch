/** "12.34" dollars to integer micro-dollars without floating point; "" is no value. */
export function dollarsToMicros(input: string): number | null {
  const text = input.trim();
  if (!text) return null;
  const match = /^(\d{1,9})(?:\.(\d{1,6}))?$/.exec(text);
  if (!match) throw new Error(`"${text}" is not an amount in dollars (for example 25 or 12.50)`);
  return Number(match[1]) * 1_000_000 + Number((match[2] || "").padEnd(6, "0"));
}

/** Integer micro-dollars as "12.50" (exact: string arithmetic, no floating point). */
export function microsToDollars(micros: number): string {
  const whole = Math.floor(micros / 1_000_000);
  const frac = String(micros % 1_000_000).padStart(6, "0").replace(/0+$/, "");
  return frac.length <= 2 ? `${whole}.${frac.padEnd(2, "0")}` : `${whole}.${frac}`;
}
