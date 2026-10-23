"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";

interface MeterMargin {
  meter: string;
  units: number;
  free_units_applied: number;
  our_cost_micros: number;
  billed_micros: number;
  margin_micros: number;
}

interface MarginResponse {
  from: string;
  to: string;
  meters: MeterMargin[];
  projects_at_limit_count: number;
  projects_near_limit_count: number;
  totals: {
    units: number;
    free_units_applied: number;
    our_cost_micros: number;
    billed_micros: number;
    margin_micros: number;
  };
}

const formatMicros = (micros: number | null | undefined): string => {
  if (micros === null || micros === undefined) return "$0.00";
  const dollars = micros / 1000000;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(dollars);
};

export default function OwnerMarginPage() {
  const [adminKey, setAdminKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [marginData, setMarginData] = useState<MarginResponse | null>(null);

  useEffect(() => {
    const savedAdminKey = localStorage.getItem("changewatch_admin_key") || "";
    const savedEndpoint =
      localStorage.getItem("changewatch_endpoint") ||
      (typeof window !== "undefined" && window.location.hostname === "localhost"
        ? "http://localhost:8000"
        : "");
    setAdminKey(savedAdminKey);
    setEndpoint(savedEndpoint);
  }, []);

  const fetchMargin = async () => {
    if (!endpoint || !adminKey) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setErrorMsg(null);

    try {
      const res = await fetch(`${endpoint.replace(/\/$/, "")}/v1/admin/margin`, {
        headers: { "X-Admin-Key": adminKey.trim() },
      });

      if (!res.ok) {
        if (res.status === 403) {
          throw new Error("Admin access denied: invalid or missing X-Admin-Key");
        }
        throw new Error(`Admin Margin API error (${res.status}): ${await res.text()}`);
      }

      const data: MarginResponse = await res.json();
      setMarginData(data);
    } catch (err: any) {
      setErrorMsg(err.message || "Failed to load margin aggregates");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (adminKey && endpoint) {
      fetchMargin();
    }
  }, [adminKey, endpoint]);

  const handleConnect = (e: React.FormEvent) => {
    e.preventDefault();
    localStorage.setItem("changewatch_admin_key", adminKey);
    localStorage.setItem("changewatch_endpoint", endpoint);
    fetchMargin();
  };

  const marginPct =
    marginData?.totals.our_cost_micros && marginData.totals.our_cost_micros > 0
      ? (
          (marginData.totals.margin_micros / marginData.totals.our_cost_micros) *
          100
        ).toFixed(1)
      : "0.0";

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      {/* Top Header */}
      <header className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-8 border-b border-slate-800 pb-4">
        <div>
          <div className="flex items-center gap-3">
            <span className="text-2xl font-black text-indigo-400">ChangeWatch</span>
            <span className="text-xs bg-indigo-950 border border-indigo-800 text-indigo-300 px-2 py-0.5 rounded font-mono font-bold">
              Owner Margin View
            </span>
          </div>
          <p className="text-sm text-slate-400 mt-1">
            Aggregate revenue, infrastructure provider cost, and gross margin across all hosted tenants
          </p>
        </div>

        <nav className="flex items-center gap-2">
          <Link
            href="/"
            className="px-3 py-1.5 text-sm rounded bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 transition"
          >
            ← Releases & Issues
          </Link>
          <Link
            href="/usage"
            className="px-3 py-1.5 text-sm rounded bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 transition"
          >
            Usage & Budgets
          </Link>
          <Link
            href="/owner/margin"
            className="px-3 py-1.5 text-sm rounded bg-indigo-950 text-indigo-300 border border-indigo-700/50 font-medium transition"
          >
            Owner Margin
          </Link>
        </nav>
      </header>

      {/* Admin Key Auth Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-6 shadow-sm">
        <form onSubmit={handleConnect} className="flex flex-wrap items-center gap-3">
          <div className="flex-1 min-w-[200px]">
            <label className="block text-xs font-mono text-slate-400 mb-1">
              API Endpoint
            </label>
            <input
              type="text"
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              placeholder="http://localhost:8000"
              className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex-1 min-w-[280px]">
            <label className="block text-xs font-mono text-slate-400 mb-1">
              Admin API Key (X-Admin-Key)
            </label>
            <input
              type="password"
              value={adminKey}
              onChange={(e) => setAdminKey(e.target.value)}
              placeholder="Admin secret configured in ADMIN_API_KEY"
              className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="self-end">
            <button
              type="submit"
              className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded transition"
            >
              Verify Admin
            </button>
          </div>
        </form>
      </div>

      {/* Strict Privacy Notice */}
      <div className="bg-indigo-950/30 border border-indigo-900/60 rounded-lg p-3.5 mb-6 text-xs text-indigo-200 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span>🔒</span>
          <span>
            <strong>Tenant Privacy Guarantee:</strong> This view is strictly restricted to platform aggregate metrics. No project identifiers, tenant names, stack traces, diffs, or customer content are accessible or displayed here.
          </span>
        </div>
      </div>

      {errorMsg && (
        <div className="bg-rose-950/40 border border-rose-800/80 text-rose-300 p-4 rounded-lg mb-6 text-sm">
          <strong>Authentication Error:</strong> {errorMsg}
        </div>
      )}

      {loading && !marginData ? (
        <div className="py-20 text-center text-slate-500 text-sm font-mono">
          Loading platform margin aggregates...
        </div>
      ) : !marginData ? (
        <div className="py-20 text-center text-slate-500 text-sm font-mono">
          Please enter a valid Admin API Key to view platform margin.
        </div>
      ) : (
        <>
          {/* Top Aggregate Cards */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Total Billed Revenue
              </span>
              <div className="text-3xl font-extrabold text-emerald-400 mt-2">
                {formatMicros(marginData.totals.billed_micros)}
              </div>
              <p className="text-xs text-slate-500 mt-1">Platform gross billings</p>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Our Provider Cost
              </span>
              <div className="text-3xl font-extrabold text-slate-200 mt-2">
                {formatMicros(marginData.totals.our_cost_micros)}
              </div>
              <p className="text-xs text-slate-500 mt-1">Direct LLM token expenses</p>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Net Margin
              </span>
              <div className="text-3xl font-extrabold text-indigo-400 mt-2">
                {formatMicros(marginData.totals.margin_micros)}
              </div>
              <p className="text-xs text-slate-500 mt-1">
                Margin percentage: <span className="font-bold text-slate-300">+{marginPct}%</span>
              </p>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Projects at / near Limit
              </span>
              <div className="text-3xl font-extrabold text-amber-400 mt-2">
                {marginData.projects_at_limit_count} / {marginData.projects_near_limit_count}
              </div>
              <p className="text-xs text-slate-500 mt-1">
                {marginData.projects_at_limit_count} at hard cap, {marginData.projects_near_limit_count} near soft limit
              </p>
            </div>
          </div>

          {/* Breakdown Table */}
          <div className="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden mb-8">
            <div className="p-4 border-b border-slate-800">
              <h3 className="font-semibold text-slate-200 text-sm">
                Margin by Meter (Aggregate)
              </h3>
              <p className="text-xs text-slate-400">
                Direct comparison between measured provider expense and billed revenue
              </p>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-950/70 border-b border-slate-800 text-slate-400 font-mono">
                  <tr>
                    <th className="py-2.5 px-4">Meter</th>
                    <th className="py-2.5 px-4">Total Units</th>
                    <th className="py-2.5 px-4">Free Applied</th>
                    <th className="py-2.5 px-4">Our Cost</th>
                    <th className="py-2.5 px-4">Billed Revenue</th>
                    <th className="py-2.5 px-4 text-right">Net Margin</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono">
                  {marginData.meters.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="py-8 text-center text-slate-500">
                        No meter activity recorded for the active period.
                      </td>
                    </tr>
                  ) : (
                    marginData.meters.map((m) => {
                      const mPct =
                        m.our_cost_micros > 0
                          ? ((m.margin_micros / m.our_cost_micros) * 100).toFixed(1)
                          : "—";
                      return (
                        <tr key={m.meter} className="hover:bg-slate-800/30">
                          <td className="py-3 px-4 font-bold text-slate-200">{m.meter}</td>
                          <td className="py-3 px-4 text-slate-300">
                            {m.units.toLocaleString()}
                          </td>
                          <td className="py-3 px-4 text-emerald-400">
                            {m.free_units_applied > 0
                              ? m.free_units_applied.toLocaleString()
                              : "0"}
                          </td>
                          <td className="py-3 px-4 text-slate-400">
                            {formatMicros(m.our_cost_micros)}
                          </td>
                          <td className="py-3 px-4 text-slate-200 font-semibold">
                            {formatMicros(m.billed_micros)}
                          </td>
                          <td className="py-3 px-4 text-right font-bold text-indigo-400">
                            {formatMicros(m.margin_micros)}{" "}
                            {mPct !== "—" && (
                              <span className="text-[10px] text-slate-500 font-normal">
                                (+{mPct}%)
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
                <tfoot className="bg-slate-950/80 border-t border-slate-800 font-mono font-bold text-slate-200">
                  <tr>
                    <td className="py-3 px-4">Totals</td>
                    <td className="py-3 px-4">
                      {marginData.totals.units.toLocaleString()}
                    </td>
                    <td className="py-3 px-4 text-emerald-400">
                      {marginData.totals.free_units_applied.toLocaleString()}
                    </td>
                    <td className="py-3 px-4 text-slate-400">
                      {formatMicros(marginData.totals.our_cost_micros)}
                    </td>
                    <td className="py-3 px-4 text-emerald-400">
                      {formatMicros(marginData.totals.billed_micros)}
                    </td>
                    <td className="py-3 px-4 text-right text-indigo-400">
                      {formatMicros(marginData.totals.margin_micros)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
