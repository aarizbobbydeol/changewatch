"use client";

import React, { useEffect, useState } from "react";
import { dollarsToMicros } from "../lib/money";
import Link from "next/link";
import { apiUrl, changeHeaders, downloadWithKey, loadEndpoint, loadReadKey, saveEndpoint, saveReadKey } from "../lib/session";
import NavLinks from "../components/NavLinks";

interface UsageItem {
  meter: string;
  units: number;
  free_units_applied: number;
  our_cost_micros: number;
  billed_micros: number;
}

interface UsageResponse {
  project_id: number;
  from: string;
  to: string;
  group: string;
  items: UsageItem[];
  totals: {
    units: number;
    free_units_applied: number;
    our_cost_micros: number;
    billed_micros: number;
  };
}

interface ForecastResponse {
  status: string;
  message?: string;
  days_elapsed?: number;
  days_with_data?: number;
  days_in_month?: number;
  current_spent_micros: number;
  daily_rate_micros?: number;
  projected_month_end_micros?: number;
}

interface BudgetResponse {
  project_id: number;
  soft_limit_micros: number | null;
  hard_limit_micros: number | null;
  warn_at: number[];
  // Names as GET /v1/budget returns them (app/metering.py get_project_budget)
  spent_micros: number;
  last_warned_pct: number;
  hard_limit_hit_at: string | null;
  optional_extras_paused: boolean;
}

interface EstimateResponse {
  meter: string;
  count: number;
  free_applicable: number;
  billable_units: number;
  estimated_billed_micros: number;
  price_book_version: number;
  pricing_mode: string;
}

const formatMicros = (micros: number | null | undefined): string => {
  if (micros === null || micros === undefined) return "$0.00";
  const dollars = micros / 1000000;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(dollars);
};

export default function UsagePage() {
  const [apiKey, setApiKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [forecast, setForecast] = useState<ForecastResponse | null>(null);
  const [budget, setBudget] = useState<BudgetResponse | null>(null);

  // Budget edit state
  const [softLimitInput, setSoftLimitInput] = useState<string>("");
  const [hardLimitInput, setHardLimitInput] = useState<string>("");
  const [savingBudget, setSavingBudget] = useState<boolean>(false);
  const [budgetSavedMsg, setBudgetSavedMsg] = useState<string | null>(null);

  // Estimate state
  const [estMeter, setEstMeter] = useState<string>("ai_investigation");
  const [estCount, setEstCount] = useState<number>(5);
  const [estimate, setEstimate] = useState<EstimateResponse | null>(null);
  const [estimating, setEstimating] = useState<boolean>(false);

  useEffect(() => {
    const savedKey = loadReadKey();
    const savedEndpoint =
      loadEndpoint();
    setApiKey(savedKey);
    setEndpoint(savedEndpoint);
  }, []);

  const fetchData = async () => {
    if (!apiKey) {
      setLoading(false);
      return;
    }

    setLoading(true);
    setErrorMsg(null);

    const headers = { "X-API-Key": apiKey.trim() };

    try {
      const [usageRes, forecastRes, budgetRes] = await Promise.all([
        fetch(`${endpoint.replace(/\/$/, "")}/v1/usage?group=meter`, { headers }),
        fetch(`${endpoint.replace(/\/$/, "")}/v1/usage/forecast`, { headers }),
        fetch(`${endpoint.replace(/\/$/, "")}/v1/budget`, { headers }),
      ]);

      if (!usageRes.ok) {
        throw new Error(
          `Usage API error (${usageRes.status}): ${await usageRes.text()}`
        );
      }
      if (!budgetRes.ok) {
        throw new Error(
          `Budget API error (${budgetRes.status}): ${await budgetRes.text()}`
        );
      }

      const usageData: UsageResponse = await usageRes.json();
      const forecastData: ForecastResponse = await forecastRes.json();
      const budgetData: BudgetResponse = await budgetRes.json();

      setUsage(usageData);
      setForecast(forecastData);
      setBudget(budgetData);

      if (budgetData.soft_limit_micros !== null) {
        setSoftLimitInput((budgetData.soft_limit_micros / 1000000).toString());
      } else {
        setSoftLimitInput("");
      }
      if (budgetData.hard_limit_micros !== null) {
        setHardLimitInput((budgetData.hard_limit_micros / 1000000).toString());
      } else {
        setHardLimitInput("");
      }
    } catch (err: any) {
      setErrorMsg(err.message || "Failed to load usage data");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (apiKey) {
      fetchData();
    }
  }, [apiKey, endpoint]);

  const handleSaveConfig = (e: React.FormEvent) => {
    e.preventDefault();
    saveReadKey(apiKey);
    saveEndpoint(endpoint);
    fetchData();
  };

  const handleUpdateBudget = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey) return;
    setSavingBudget(true);
    setBudgetSavedMsg(null);

    try {
      const soft = dollarsToMicros(softLimitInput);
      const hard = dollarsToMicros(hardLimitInput);

      const res = await fetch(`${endpoint.replace(/\/$/, "")}/v1/budget`, {
        method: "PUT",
        // The manage key (entered on the Settings page), or the read key with the admin key
        headers: changeHeaders(apiKey.trim()),
        body: JSON.stringify({
          soft_limit_micros: soft,
          hard_limit_micros: hard,
          // Keep the project's own warning levels (sending the defaults reset them)
          warn_at: budget?.warn_at?.length ? budget.warn_at : [50, 80, 100],
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
        throw new Error(`Could not update the budget: ${detail}${res.status === 403 ? " (enter it on the Settings page)" : ""}`);
      }

      const updated = await res.json();
      setBudget(updated);
      setBudgetSavedMsg("Budget settings saved successfully.");
      setTimeout(() => setBudgetSavedMsg(null), 4000);
    } catch (err: any) {
      setErrorMsg(err.message || "Could not save budget");
    } finally {
      setSavingBudget(false);
    }
  };

  const handleRunEstimate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey) return;
    setEstimating(true);
    try {
      const res = await fetch(
        `${endpoint.replace(/\/$/, "")}/v1/usage/estimate?meter=${encodeURIComponent(
          estMeter
        )}&count=${estCount}`,
        { headers: { "X-API-Key": apiKey.trim() } }
      );
      if (res.ok) {
        const data = await res.json();
        setEstimate(data);
      }
    } catch (err) {
      // ignore
    } finally {
      setEstimating(false);
    }
  };

  // Budget progress math
  const currentSpent = budget?.spent_micros ?? usage?.totals.billed_micros ?? 0;
  const hardLimit = budget?.hard_limit_micros;
  const softLimit = budget?.soft_limit_micros;
  const effectiveLimit = hardLimit || softLimit || 0;
  const spendPct =
    effectiveLimit > 0 ? Math.min(100, Math.round((currentSpent / effectiveLimit) * 100)) : 0;

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      {/* Top Navigation */}
      <header className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-8 border-b border-slate-800 pb-4">
        <div>
          <div className="flex items-center gap-3">
            <span className="text-2xl font-black text-emerald-400">ChangeWatch</span>
            <span className="text-xs bg-slate-800 border border-slate-700 text-slate-300 px-2 py-0.5 rounded font-mono">
              Usage & Metering
            </span>
          </div>
          <p className="text-sm text-slate-400 mt-1">
            Cost-plus transparent usage tracking, forecast, and budget enforcement
          </p>
        </div>

        <nav className="flex flex-wrap items-center gap-2">
          <NavLinks />
        </nav>
      </header>

      {/* API Key Configuration Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 mb-6 shadow-sm">
        <form onSubmit={handleSaveConfig} className="flex flex-wrap items-center gap-3">
          <div className="flex-1 min-w-[200px]">
            <label className="block text-xs font-mono text-slate-400 mb-1">
              API Endpoint
            </label>
            <input
              type="text"
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              placeholder="http://localhost:8000"
              className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
            />
          </div>
          <div className="flex-1 min-w-[280px]">
            <label className="block text-xs font-mono text-slate-400 mb-1">
              Read API Key
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="cw_read_..."
              className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
            />
          </div>
          <div className="self-end">
            <button
              type="submit"
              className="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded transition"
            >
              Connect
            </button>
          </div>
        </form>
      </div>

      {errorMsg && (
        <div className="bg-rose-950/40 border border-rose-800/80 text-rose-300 p-4 rounded-lg mb-6 text-sm">
          <strong>Error:</strong> {errorMsg}
        </div>
      )}

      {/* Paused Extras Banner */}
      {budget?.optional_extras_paused && (
        <div className="bg-amber-950/50 border border-amber-600 text-amber-200 p-4 rounded-lg mb-6 flex items-start gap-3">
          <span className="text-xl">⚠️</span>
          <div>
            <h4 className="font-semibold text-amber-100">AI Investigations Paused</h4>
            <p className="text-xs text-amber-300 mt-0.5">
              Monthly spend reached the hard budget limit. Optional AI investigations and extra event storage are paused.
              The safety floor remains 100% active: verdicts, instant alerts, and dead-man&apos;s switch continue without interruption.
              Raise the budget below or wait for next month to resume extras immediately.
            </p>
          </div>
        </div>
      )}

      {loading && !usage ? (
        <div className="py-20 text-center text-slate-500 text-sm">Loading usage ledger...</div>
      ) : (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Spend This Month
              </span>
              <div className="text-3xl font-extrabold text-slate-100 mt-2">
                {formatMicros(currentSpent)}
              </div>
              <p className="text-xs text-slate-500 mt-1">
                Measured in integer micro-USD. 0 floats.
              </p>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Month-End Forecast
              </span>
              <div className="text-3xl font-extrabold text-emerald-400 mt-2">
                {forecast?.status === "ok"
                  ? formatMicros(forecast.projected_month_end_micros)
                  : "Not enough data"}
              </div>
              <p className="text-xs text-slate-500 mt-1">
                {forecast?.status === "ok"
                  ? `Linear burn rate: ${formatMicros(forecast.daily_rate_micros)} / day`
                  : "Requires at least 3 days of data in current month"}
              </p>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
                Active Budget Limit
              </span>
              <div className="text-3xl font-extrabold text-slate-200 mt-2">
                {effectiveLimit > 0 ? formatMicros(effectiveLimit) : "None"}
              </div>
              <p className="text-xs text-slate-500 mt-1">
                {hardLimit
                  ? `Hard limit: ${formatMicros(hardLimit)} (pauses extras)`
                  : softLimit
                  ? `Soft limit: ${formatMicros(softLimit)} (warning only)`
                  : "Self-hosted: unlimited by default"}
              </p>
            </div>
          </div>

          {/* Budget Progress Bar */}
          {effectiveLimit > 0 && (
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5 mb-6">
              <div className="flex justify-between items-center mb-2">
                <span className="text-xs font-mono text-slate-300">
                  Budget Utilization: {spendPct}%
                </span>
                <span className="text-xs font-mono text-slate-400">
                  {formatMicros(currentSpent)} / {formatMicros(effectiveLimit)}
                </span>
              </div>
              <div className="w-full bg-slate-950 h-3 rounded-full overflow-hidden relative border border-slate-800">
                <div
                  className={`h-full transition-all duration-500 ${
                    spendPct >= 100
                      ? "bg-rose-500"
                      : spendPct >= 80
                      ? "bg-amber-500"
                      : "bg-emerald-500"
                  }`}
                  style={{ width: `${Math.min(100, spendPct)}%` }}
                />
              </div>
              <div className="flex justify-between text-[10px] text-slate-500 font-mono mt-1.5 px-0.5">
                <span>0%</span>
                <span className="text-amber-400/80">Warn 50%</span>
                <span className="text-amber-400/80">Warn 80%</span>
                <span className="text-rose-400/80">Cap 100%</span>
              </div>
            </div>
          )}

          {/* Usage by Meter Table */}
          <div className="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden mb-6">
            <div className="p-4 border-b border-slate-800 flex justify-between items-center">
              <div>
                <h3 className="font-semibold text-slate-200 text-sm">Monthly Usage by Meter</h3>
                <p className="text-xs text-slate-400">
                  Real recorded units and free tier allowances applied
                </p>
              </div>
              <button
                type="button"
                onClick={() =>
                  downloadWithKey(apiUrl(endpoint, "/v1/usage/export.csv"), apiKey, "changewatch-usage.csv").catch((err) =>
                    setErrorMsg(err.message)
                  )
                }
                className="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs rounded border border-slate-700 transition"
              >
                Export CSV
              </button>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-950/70 border-b border-slate-800 text-slate-400 font-mono">
                  <tr>
                    <th className="py-2.5 px-4">Meter</th>
                    <th className="py-2.5 px-4">Pricing Mode</th>
                    <th className="py-2.5 px-4">Units</th>
                    <th className="py-2.5 px-4">Free Allowance Used</th>
                    <th className="py-2.5 px-4 text-right">Billed Amount</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 font-mono">
                  {usage?.items.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-8 text-center text-slate-500">
                        No billable events recorded this calendar month.
                      </td>
                    </tr>
                  ) : (
                    usage?.items.map((it) => (
                      <tr key={it.meter} className="hover:bg-slate-800/30">
                        <td className="py-3 px-4 font-semibold text-slate-200">
                          {it.meter === "ai_investigation"
                            ? "AI Investigation (Estimated Provider Cost)"
                            : it.meter === "ai_investigation_deep"
                            ? "AI Investigation Deep (Estimated Provider Cost)"
                            : it.meter === "release_monitored"
                            ? "Releases Monitored (Fixed)"
                            : it.meter === "stored_events"
                            ? "Stored Events (Batched)"
                            : it.meter}
                        </td>
                        <td className="py-3 px-4 text-slate-400">
                          {it.meter.startsWith("ai_") ? "Cost-plus (1.30 margin)" : "Fixed"}
                        </td>
                        <td className="py-3 px-4 text-slate-300">{it.units.toLocaleString()}</td>
                        <td className="py-3 px-4 text-emerald-400">
                          {it.free_units_applied > 0
                            ? `${it.free_units_applied.toLocaleString()} free applied`
                            : "—"}
                        </td>
                        <td className="py-3 px-4 text-right font-bold text-slate-100">
                          {formatMicros(it.billed_micros)}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
                <tfoot className="bg-slate-950/80 border-t border-slate-800 font-mono font-bold text-slate-200">
                  <tr>
                    <td className="py-3 px-4" colSpan={2}>
                      Total
                    </td>
                    <td className="py-3 px-4">
                      {usage?.totals.units.toLocaleString() || 0}
                    </td>
                    <td className="py-3 px-4 text-emerald-400">
                      {usage?.totals.free_units_applied.toLocaleString() || 0} free applied
                    </td>
                    <td className="py-3 px-4 text-right text-emerald-400">
                      {formatMicros(usage?.totals.billed_micros || 0)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>

          {/* Budget Settings & Cost Estimate Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            {/* Budget Configuration */}
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <h3 className="font-semibold text-slate-200 text-sm mb-1">
                Budget Limits & Safeguards
              </h3>
              <p className="text-xs text-slate-400 mb-4">
                Changes apply immediately. Hard limits pause optional AI investigations and extra event storage, never verdicts or alerts.
              </p>

              {budgetSavedMsg && (
                <div className="text-xs bg-emerald-950/60 border border-emerald-700 text-emerald-300 p-2.5 rounded mb-3">
                  {budgetSavedMsg}
                </div>
              )}

              <form onSubmit={handleUpdateBudget} className="space-y-3">
                <div>
                  <label className="block text-xs font-mono text-slate-300 mb-1">
                    Soft Limit (USD) — Warning Only
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    placeholder="e.g. 5.00 (leave blank for unlimited)"
                    value={softLimitInput}
                    onChange={(e) => setSoftLimitInput(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
                  />
                </div>

                <div>
                  <label className="block text-xs font-mono text-slate-300 mb-1">
                    Hard Limit (USD) — Pauses Optional Extras
                  </label>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    placeholder="e.g. 10.00 (leave blank for unlimited)"
                    value={hardLimitInput}
                    onChange={(e) => setHardLimitInput(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
                  />
                </div>

                <div className="pt-2">
                  <button
                    type="submit"
                    disabled={savingBudget}
                    className="w-full py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-semibold rounded transition"
                  >
                    {savingBudget ? "Saving..." : "Save Budget Limits"}
                  </button>
                </div>
              </form>
            </div>

            {/* Quick Cost Estimator */}
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-5">
              <h3 className="font-semibold text-slate-200 text-sm mb-1">
                Cost Estimator
              </h3>
              <p className="text-xs text-slate-400 mb-4">
                Calculate estimated cost before running, factoring in remaining monthly free allowances.
              </p>

              <form onSubmit={handleRunEstimate} className="space-y-3">
                <div>
                  <label className="block text-xs font-mono text-slate-300 mb-1">
                    Meter Action
                  </label>
                  <select
                    value={estMeter}
                    onChange={(e) => setEstMeter(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
                  >
                    <option value="ai_investigation">AI Investigation (Fast)</option>
                    <option value="ai_investigation_deep">AI Investigation (Deep)</option>
                    <option value="release_monitored">Release Monitored</option>
                    <option value="stored_events">Stored Events</option>
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-mono text-slate-300 mb-1">
                    Quantity / Count
                  </label>
                  <input
                    type="number"
                    min="1"
                    value={estCount}
                    onChange={(e) => setEstCount(parseInt(e.target.value, 10) || 1)}
                    className="w-full bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"
                  />
                </div>

                <div className="pt-2">
                  <button
                    type="submit"
                    disabled={estimating}
                    className="w-full py-2 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-200 text-xs font-semibold rounded transition"
                  >
                    {estimating ? "Calculating..." : "Estimate Cost"}
                  </button>
                </div>

                {estimate && (
                  <div className="mt-3 p-3 bg-slate-950 border border-slate-800 rounded text-xs font-mono space-y-1">
                    <div className="flex justify-between text-slate-400">
                      <span>Free allowance applicable:</span>
                      <span className="text-emerald-400">{estimate.free_applicable} units</span>
                    </div>
                    <div className="flex justify-between text-slate-400">
                      <span>Billable units:</span>
                      <span className="text-slate-200">{estimate.billable_units} units</span>
                    </div>
                    <div className="flex justify-between text-slate-200 font-bold border-t border-slate-800 pt-1 mt-1">
                      <span>Estimated Billed:</span>
                      <span className="text-emerald-400">
                        {formatMicros(estimate.estimated_billed_micros)}
                      </span>
                    </div>
                  </div>
                )}
              </form>
            </div>
          </div>

          {/* Pricing Model Note */}
          <div className="bg-slate-900/60 border border-slate-800/60 rounded-lg p-5 text-xs text-slate-400 space-y-2">
            <h4 className="font-semibold text-slate-300">How ChangeWatch Cost-Plus Pricing Works</h4>
            <p>
              Pricing is pay-as-you-go with minimal margin: <code className="text-slate-300 bg-slate-800 px-1 py-0.5 rounded">price = measured cost + configurable margin</code>.
              AI investigation costs are calculated directly from real token counts consumed from your configured LLM provider.
              Local models (<code className="text-slate-300 bg-slate-800 px-1 py-0.5 rounded">ollama</code>) and fakes cost $0 and bill $0.
            </p>
            <p>
              Free allowances are granted every calendar month per project (e.g. 200 free releases, 20 free AI investigations, 100k free events).
              Unused free allowances do not roll over.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
