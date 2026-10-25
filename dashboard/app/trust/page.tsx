"use client";

import React, { useEffect, useState } from "react";
import NavLinks from "../components/NavLinks";
import { apiUrl, loadEndpoint, loadReadKey } from "../lib/session";

interface TrustMetrics {
  period_days: number;
  total_alerts: number;
  rated_alerts: number;
  coverage: number;
  precision: number | null;
  sample_ok: boolean;
  counts: { real: number; false: number; unclear: number };
  median_ttd_sec: number | null;
  median_ttr_min: number | null;
}

interface CalibrationBin {
  bin: string;
  samples: number;
  observed_rate: number | null;
  visible: boolean;
}

interface TrustSummary {
  metrics: TrustMetrics;
  // As /v1/trust/summary returns them (trust/feedback.py get_feedback_suggestions)
  suggestions: { kind?: string; pattern?: string; reason?: string; false_count?: number; suggestion_text?: string }[];
  calibration: { headline: string; status: string; bins: CalibrationBin[]; total_samples: number; brier_score: number | null };
}

interface UptimeTile {
  formatted: string;
  sample_ok: boolean;
  note?: string;
}

function formatSeconds(sec: number | null): string {
  if (sec === null || sec === undefined) return "Not enough data yet";
  if (sec < 60) return `${sec.toFixed(1)} s`;
  return `${(sec / 60).toFixed(1)} min`;
}

export default function TrustPage() {
  const [readKey, setReadKey] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [summary, setSummary] = useState<TrustSummary | null>(null);
  const [uptime, setUptime] = useState<UptimeTile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setReadKey(loadReadKey());
    setEndpoint(loadEndpoint());
  }, []);

  useEffect(() => {
    if (!readKey) return;
    let cancelled = false;
    const headers = { "X-API-Key": readKey };
    setLoading(true);
    setError(null);
    Promise.all([
      fetch(apiUrl(endpoint, "/v1/trust/summary"), { headers }),
      fetch(apiUrl(endpoint, "/v1/analytics/overview?period=30"), { headers }),
    ])
      .then(async ([trustRes, overviewRes]) => {
        if (!trustRes.ok) throw new Error(trustRes.status === 401 || trustRes.status === 403 ? "That read key was not accepted." : `The API answered ${trustRes.status}.`);
        const trust: TrustSummary = await trustRes.json();
        const overview = overviewRes.ok ? await overviewRes.json() : null;
        if (!cancelled) {
          setSummary(trust);
          setUptime(overview?.kpis?.tool_uptime ?? null);
        }
      })
      .catch((err: Error) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [readKey, endpoint]);

  const m = summary?.metrics;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30 px-6 py-3.5 flex flex-wrap items-center gap-4 justify-between">
        <h1 className="text-lg font-semibold">Trust</h1>
        <nav className="flex flex-wrap items-center gap-2">
          <NavLinks />
        </nav>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-8 space-y-8">
        <p className="text-sm text-slate-400">
          How often ChangeWatch&apos;s alerts were right, how fast they came, and whether its confidence matches reality. Numbers appear only once there is enough data to mean something.
        </p>

        {!readKey && (
          <p className="text-sm text-slate-400">
            Add your read key on the <a href="/settings" className="text-teal-300 underline">Settings</a> page first.
          </p>
        )}
        {loading && <p className="text-sm text-slate-400">Loading…</p>}
        {error && <p className="text-sm text-rose-400">{error}</p>}

        {m && !loading && (
          <>
            <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <p className="text-xs text-slate-400">Alerts that were real</p>
                <p className="mt-1 text-2xl font-semibold">
                  {m.sample_ok && m.precision !== null ? `${Math.round(m.precision * 100)}%` : "Not enough data yet"}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {m.rated_alerts} of {m.total_alerts} alerts rated ({Math.round(m.coverage * 100)}% coverage)
                </p>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <p className="text-xs text-slate-400">Alert speed (median)</p>
                <p className="mt-1 text-2xl font-semibold">{formatSeconds(m.median_ttd_sec)}</p>
                <p className="mt-1 text-xs text-slate-500">First error to alert, last {m.period_days} days</p>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <p className="text-xs text-slate-400">Time to resolve (median)</p>
                <p className="mt-1 text-2xl font-semibold">{m.median_ttr_min === null ? "Not enough data yet" : `${m.median_ttr_min.toFixed(0)} min`}</p>
                <p className="mt-1 text-xs text-slate-500">Incident opened to resolved</p>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
                <p className="text-xs text-slate-400">ChangeWatch uptime</p>
                <p className="mt-1 text-2xl font-semibold">{uptime ? uptime.formatted : "Not enabled"}</p>
                <p className="mt-1 text-xs text-slate-500">{uptime?.note || "From the watchdog's component checks"}</p>
              </div>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Calibration</h2>
              <p className="text-sm text-slate-400 mb-4">
                {summary?.calibration.headline}. When ChangeWatch says it is about 90% sure an error came from the release, it should be right about 90% of the time.
              </p>
              <table className="w-full text-sm">
                <thead className="text-left text-slate-400">
                  <tr>
                    <th className="py-1 font-normal">When it said</th>
                    <th className="py-1 font-normal">Samples</th>
                    <th className="py-1 font-normal">It was right</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {summary?.calibration.bins.map((b) => (
                    <tr key={b.bin}>
                      <td className="py-2">{b.bin}</td>
                      <td className="py-2 text-slate-300">{b.samples}</td>
                      <td className="py-2 text-slate-300">
                        {b.visible && b.observed_rate !== null ? `${Math.round(b.observed_rate * 100)}%` : "Not enough data yet"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Feedback so far</h2>
              <p className="text-sm text-slate-400">
                Real: {m.counts.real} · False alarm: {m.counts.false} · Unclear: {m.counts.unclear}. Rate alerts from the incident page to improve these numbers.
              </p>
              {summary && summary.suggestions.length > 0 && (
                <ul className="mt-3 list-disc pl-5 text-sm text-slate-300 space-y-1">
                  {summary.suggestions.map((s, i) => (
                    <li key={i}>{s.suggestion_text || s.reason || s.pattern}</li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
