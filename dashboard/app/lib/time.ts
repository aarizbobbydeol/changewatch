/** A log line's time (seconds since 1970) as HH:MM:SS in UTC. A value no Date can hold, such as a
 * time a client sent in nanoseconds, shows as "--:--:--": toISOString() would throw and take the
 * whole incident page down with it. */
export function logClock(ts: number | undefined): string {
  const date = new Date(Number(ts) * 1000);
  return isNaN(date.getTime()) ? "--:--:--" : date.toISOString().slice(11, 19);
}
