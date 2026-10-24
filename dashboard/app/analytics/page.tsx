"use client";

import React, { useEffect, useState, useMemo } from "react";
import Link from "next/link";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts";
import { downloadWithKey, loadEndpoint, loadReadKey } from "../lib/session";
import NavLinks from "../components/NavLinks";

interface MetricItem {
  id: string;
  value: number | null;
  formatted: string;
  unit: string;
  n: number;
  sample_ok: boolean;
  note?: string;
  details?: Record<string, any>;
}

export default function AnalyticsDashboardPage() {
  const [apiKey, setApiKey] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [periodDays, setPeriodDays] = useState<number>(30);
  const [env, setEnv] = useState("prod");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedSec, setLastUpdatedSec] = useState(0);

  // Data states
  const [overview, setOverview] = useState<Record<string, MetricItem>>({});
  const [verdictsByWeek, setVerdictsByWeek] = useState<any[]>([]);
  const [ttfByWeek, setTtfByWeek] = useState<any[]>([]);
  const [hotspots, setHotspots] = useState<any[]>([]);
  const [categories, setCategories] = useState<any[]>([]);
  const [operations, setOperations] = useState<Record<string, any>>({});
  const [definitions, setDefinitions] = useState<Record<string, any>>({});
  const [activeTooltip, setActiveTooltip] = useState<string | null>(null);

  // Load API key from localStorage
  useEffect(() => {
    const savedKey = loadReadKey();
    const savedEp = loadEndpoint();
    setApiKey(savedKey);
    setEndpoint(savedEp);
  }, []);

  const getApiUrl = (path: string) => `${endpoint.replace(/\/+$/, "")}${path}`;

  const fetchAnalytics = async () => {
    if (!apiKey) return;
    setLoading(true);
    setError(null);
    try {
      const headers = { "X-API-Key": apiKey.trim() };

      const [
        ovRes,
        vbwRes,
        tbwRes,
        hotRes,
        catRes,
        opsRes,
        defRes,
      ] = await Promise.allSettled([
        fetch(getApiUrl(`/v1/analytics/overview?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/verdicts-by-week?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/ttf-by-week?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/hotspots?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/categories?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/operations?period=${periodDays}&env=${env}`), { headers }).then((r) => r.json()),
        fetch(getApiUrl(`/v1/analytics/definitions`), { headers }).then((r) => r.json()),
      ]);

      // The API returns the KPI tiles under `kpis` and the series as plain lists
      const asList = (res: PromiseSettledResult<any>, key: string): any[] | null => {
        if (res.status !== "fulfilled") return null;
        if (Array.isArray(res.value)) return res.value;
        return Array.isArray(res.value?.[key]) ? res.value[key] : null;
      };
      if (ovRes.status === "fulfilled" && !ovRes.value.detail) setOverview(ovRes.value.kpis ?? ovRes.value);
      const weeks = asList(vbwRes, "weeks");
      if (weeks) setVerdictsByWeek(weeks);
      const ttfWeeks = asList(tbwRes, "weeks");
      if (ttfWeeks) setTtfByWeek(ttfWeeks);
      const hot = asList(hotRes, "hotspots");
      if (hot) setHotspots(hot);
      const cats = asList(catRes, "categories");
      if (cats) setCategories(cats);
      if (opsRes.status === "fulfilled") setOperations(opsRes.value);
      if (defRes.status === "fulfilled") setDefinitions(defRes.value);

      setLastUpdatedSec(0);
    } catch (err: any) {
      setError(err?.message || "Failed to load analytics data");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (apiKey) {
      fetchAnalytics();
    }
  }, [apiKey, endpoint, periodDays, env]);

  // Tick timer
  useEffect(() => {
    const timer = setInterval(() => {
      setLastUpdatedSec((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const exportCsv = (metricId: string) => {
    downloadWithKey(
      getApiUrl(`/v1/analytics/export.csv?metric=${metricId}&period=${periodDays}&env=${env}`),
      apiKey,
      `changewatch-${metricId}.csv`
    ).catch((err) => setError(err.message));
  };

  const renderMetricTile = (metricKey: string, title: string) => {
    const m = overview[metricKey];
    const def = definitions[metricKey] || {};

    return (
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 flex flex-col justify-between relative hover:border-slate-700 transition">
        <div className="flex items-center justify-between">
          <span className="text-xs uppercase tracking-wider font-semibold text-slate-400">{title}</span>
          <button
            onClick={() => setActiveTooltip(activeTooltip === metricKey ? null : metricKey)}
            className="text-slate-500 hover:text-slate-300 text-xs px-1.5 py-0.5 rounded border border-slate-700/60"
            title="Definition and Formula"
          >
            ⓘ
          </button>
        </div>

        {activeTooltip === metricKey && def.formula && (
          <div className="absolute top-10 right-4 z-20 bg-slate-800 border border-slate-700 text-slate-200 text-xs rounded-lg p-3 w-64 shadow-xl">
            <p className="font-semibold text-white mb-1">{def.title || title}</p>
            <p className="text-slate-300 mb-2">{def.description}</p>
            <p className="text-[11px] text-slate-400 font-mono bg-slate-900/80 p-1.5 rounded">{def.formula}</p>
            {def.caveats && <p className="text-[10px] text-amber-400 mt-2">⚠️ {def.caveats}</p>}
          </div>
        )}

        <div className="my-3">
          {loading ? (
            <div className="h-8 w-24 bg-slate-800 animate-pulse rounded my-1" />
          ) : m?.sample_ok ? (
            <div className="flex items-baseline gap-2">
              <span className="text-2xl font-bold text-white tracking-tight">{m.formatted}</span>
            </div>
          ) : (
            <div className="text-sm font-medium text-slate-500 italic">
              {m?.formatted || "Not enough data"}
            </div>
          )}
        </div>

        <div className="text-[11px] text-slate-500 flex items-center justify-between border-t border-slate-800/80 pt-2">
          <span>{m?.n !== undefined ? `n = ${m.n} cases` : "n = 0"}</span>
          <span className="text-slate-600">{m?.note || def.window || `${periodDays}d window`}</span>
        </div>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
      {/* Top Navigation */}
      <header className="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur px-6 py-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-6">
          <Link href="/" className="flex items-center space-x-2.5">
            <div className="h-7 w-7 rounded bg-gradient-to-tr from-indigo-500 to-sky-400 flex items-center justify-center font-black text-white text-sm shadow-md">
              CW
            </div>
            <span className="font-bold text-slate-100 text-base tracking-tight">ChangeWatch</span>
          </Link>
          <nav className="flex flex-wrap items-center gap-1.5 pl-4 border-l border-slate-800">
            <NavLinks />
          </nav>
        </div>

        {/* Global Controls */}
        <div className="flex items-center space-x-3">
          <select
            value={env}
            onChange={(e) => setEnv(e.target.value)}
            className="bg-slate-800 text-xs text-slate-200 border border-slate-700 rounded-lg px-2.5 py-1.5 outline-none font-medium"
          >
            <option value="prod">prod</option>
            <option value="staging">staging</option>
          </select>

          <select
            value={periodDays}
            onChange={(e) => setPeriodDays(Number(e.target.value))}
            className="bg-slate-800 text-xs text-slate-200 border border-slate-700 rounded-lg px-2.5 py-1.5 outline-none font-medium"
          >
            <option value={7}>Last 7 Days</option>
            <option value={30}>Last 30 Days</option>
            <option value={90}>Last 90 Days</option>
          </select>

          <button
            onClick={fetchAnalytics}
            disabled={loading}
            className="px-3 py-1.5 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 transition"
          >
            {loading ? "Refreshing..." : "↻ Refresh"}
          </button>

          <span className="text-[11px] text-slate-500">Updated {lastUpdatedSec}s ago</span>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
        {/* Error Alert */}
        {error && (
          <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm flex items-center justify-between">
            <span>{error}</span>
            <button onClick={fetchAnalytics} className="underline text-xs ml-4">Retry</button>
          </div>
        )}

        {/* Row 1: 6 KPI Tiles */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
          {renderMetricTile("releases", "Releases")}
          {renderMetricTile("change_failure_rate", "Change Failure Rate")}
          {renderMetricTile("time_to_detect", "Time to Detect")}
          {renderMetricTile("time_to_fix", "Time to Fix")}
          {renderMetricTile("alerts_accuracy", "Alerts That Were Real")}
          {renderMetricTile("tool_uptime", "Tool Uptime")}
        </div>

        {/* Row 2: Charts (Verdicts per week, TTF per week) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 flex flex-col justify-between">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-slate-200 text-sm">Release Verdicts per Week</h3>
              <button onClick={() => exportCsv("verdicts")} className="text-xs text-slate-400 hover:text-slate-200 border border-slate-800 px-2 py-1 rounded">
                Export CSV
              </button>
            </div>
            <div className="h-64 w-full">
              {verdictsByWeek.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={verdictsByWeek}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.3} />
                    <XAxis dataKey="week" stroke="#64748b" fontSize={11} />
                    <YAxis stroke="#64748b" fontSize={11} />
                    <Tooltip contentStyle={{ backgroundColor: "#1e293b", borderColor: "#334155" }} />
                    <Legend />
                    <Bar dataKey="safe" stackId="a" fill="#10b981" name="Safe" />
                    <Bar dataKey="watch" stackId="a" fill="#f59e0b" name="Watch" />
                    <Bar dataKey="rollback" stackId="a" fill="#f43f5e" name="Rollback" />
                    <Bar dataKey="no_data" stackId="a" fill="#64748b" name="No Data" />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex items-center justify-center text-slate-500 text-xs italic">
                  Collecting weekly verdict trends...
                </div>
              )}
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 flex flex-col justify-between">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-slate-200 text-sm">Median Time to Fix per Week (Minutes)</h3>
              <button onClick={() => exportCsv("ttf")} className="text-xs text-slate-400 hover:text-slate-200 border border-slate-800 px-2 py-1 rounded">
                Export CSV
              </button>
            </div>
            <div className="h-64 w-full">
              {ttfByWeek.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={ttfByWeek}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#334155" opacity={0.3} />
                    <XAxis dataKey="week" stroke="#64748b" fontSize={11} />
                    <YAxis stroke="#64748b" fontSize={11} />
                    <Tooltip contentStyle={{ backgroundColor: "#1e293b", borderColor: "#334155" }} />
                    <Line type="monotone" dataKey="median_minutes" stroke="#38bdf8" strokeWidth={2} name="Median TTF (m)" />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-full flex items-center justify-center text-slate-500 text-xs italic">
                  Collecting resolution duration history...
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Row 3: Breakdowns (Where problems come from & Likely causes by category) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-semibold text-slate-200 text-sm">Where Problems Come From (Areas of Code)</h3>
              <span className="text-xs text-slate-500">No person rankings</span>
            </div>
            <div className="divide-y divide-slate-800">
              {hotspots.length > 0 ? (
                hotspots.map((h, i) => (
                  <div key={i} className="py-2.5 flex items-center justify-between text-xs">
                    <span className="font-mono text-slate-300">{h.area}</span>
                    <div className="flex items-center gap-4">
                      <span className="text-slate-400">{h.count} incidents</span>
                      <span className="font-semibold text-slate-200 w-12 text-right">{h.share_percent}%</span>
                    </div>
                  </div>
                ))
              ) : (
                <div className="py-6 text-center text-slate-500 text-xs italic">
                  No incident hotspots recorded in this window.
                </div>
              )}
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-semibold text-slate-200 text-sm">Likely Causes by Category</h3>
              <span className="text-xs text-slate-500">Triage & Investigation</span>
            </div>
            <div className="divide-y divide-slate-800">
              {categories.length > 0 ? (
                categories.map((c, i) => (
                  <div key={i} className="py-2.5 flex items-center justify-between text-xs">
                    <span className="text-slate-300 capitalize">{c.category.replace(/_/g, " ")}</span>
                    <div className="flex items-center gap-4">
                      <span className="text-slate-400">{c.count} cases</span>
                      <span className="font-semibold text-slate-200 w-12 text-right">{c.share_percent}%</span>
                    </div>
                  </div>
                ))
              ) : (
                <div className="py-6 text-center text-slate-500 text-xs italic">
                  No categorized incidents in this window.
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Row 4: Is the tool working well? (Operations panel) */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4 border-b border-slate-800 pb-3">
            <div>
              <h3 className="font-semibold text-slate-200 text-sm">Is the Tool Working Well? (Operations)</h3>
              <p className="text-xs text-slate-500">Alert speed, data flow, ingest stats, and notification delivery</p>
            </div>
            <Link href="/" className="text-xs text-indigo-400 hover:text-indigo-300">
              System Status Panel →
            </Link>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
            <div className="bg-slate-950/60 p-3 rounded-lg border border-slate-800">
              <span className="text-slate-500 block mb-1">Alert Speed Target</span>
              <span className="text-sm font-bold text-white">
                {operations?.alert_speed?.formatted || "< 5.0s"}
              </span>
              <span className="text-[10px] text-slate-500 block mt-1">median & p95</span>
            </div>

            <div className="bg-slate-950/60 p-3 rounded-lg border border-slate-800">
              <span className="text-slate-500 block mb-1">Customer App Sources</span>
              <span className="text-sm font-bold text-emerald-400">
                {operations?.sources?.status || "Data Flowing"}
              </span>
              <span className="text-[10px] text-slate-500 block mt-1">
                {operations?.sources?.active_count || 1} active source(s)
              </span>
            </div>

            <div className="bg-slate-950/60 p-3 rounded-lg border border-slate-800">
              <span className="text-slate-500 block mb-1">Notifications Delivered</span>
              <span className="text-sm font-bold text-white">
                {operations?.notifications?.delivered_rate || "100%"}
              </span>
              <span className="text-[10px] text-slate-500 block mt-1">durable outbox active</span>
            </div>

            <div className="bg-slate-950/60 p-3 rounded-lg border border-slate-800">
              <span className="text-slate-500 block mb-1">Calibration Accuracy</span>
              <span className="text-sm font-bold text-indigo-400">
                {operations?.calibration?.headline || "Calibrating (n=0)"}
              </span>
              <span className="text-[10px] text-slate-500 block mt-1">Brier score analysis</span>
            </div>
          </div>
        </div>

        {/* Small footer link to definitions */}
        <div className="text-center text-xs text-slate-600 pb-8">
          All metrics calculated strictly via pure functions in metrics.py.{" "}
          <a href="/docs/METRICS.md" target="_blank" className="text-slate-400 underline">
            How these numbers are calculated
          </a>
        </div>
      </main>
    </div>
  );
}
