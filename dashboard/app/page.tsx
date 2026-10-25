"use client";

import React, { useEffect, useState, useMemo } from "react";
import Link from "next/link";
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts";
import { loadEndpoint, loadReadKey, saveEndpoint, saveReadKey } from "./lib/session";
import NavLinks from "./components/NavLinks";

interface Deploy {
  id: number;
  commit_sha: string;
  author: string | null;
  message: string | null;
  diff: string | null;
  environment: string;
  deployed_at: string;
  verdict: "safe" | "watch" | "rollback" | "no_data";
  verdict_reasons: string[];
  verdict_final: boolean;
  verdict_updated_at: string | null;
  likely_cause: string | null;
  new_issues: number;
  errors_before: number;
  errors_after: number;
  flagged: boolean;
}

interface IssueEvent {
  id: number;
  occurred_at: string;
  stack_trace: string | null;
  request_path: string | null;
  duration_ms: number | null;
  extra: Record<string, any>;
  deploy_id: number | null;
  commit_sha: string | null;
}

interface Issue {
  id: number;
  fingerprint: string;
  title: string;
  first_seen: string;
  last_seen: string;
  status: string;
  event_count: number;
  events?: IssueEvent[];
}

interface TimelineDeploy {
  id: number;
  commit_sha: string;
  environment: string;
  verdict: string;
  deployed_at?: string;
}

interface TimelineBucket {
  bucket: string;
  timestamp: string;
  requests: number;
  errors: number;
  deploys: TimelineDeploy[];
}

const formatTime = (d?: string | null): string => {
  if (!d) return "N/A";
  const date = new Date(d);
  return isNaN(date.getTime()) ? "N/A" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

const formatDateTime = (d?: string | null): string => {
  if (!d) return "N/A";
  const date = new Date(d);
  return isNaN(date.getTime()) ? "N/A" : date.toLocaleString();
};

interface AiCostWindow {
  calls: number;
  cost: string;
  unpriced_calls: number;
  per_1k_requests: string | null;
  per_call: string | null;
}

interface AiCost {
  deploy_id: number;
  basis: "per_1k_requests" | "per_call" | null;
  change: string | null;
  jumped: boolean;
  before: AiCostWindow;
  after: AiCostWindow;
  top: { model: string; feature: string | null; calls: number; cost: string }[];
}

export default function DashboardPage() {
  const [apiKey, setApiKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [timeRangeHours, setTimeRangeHours] = useState<number>(24);
  const [activeTab, setActiveTab] = useState<"deploys" | "issues">("deploys");
  const [isMounted, setIsMounted] = useState<boolean>(false);

  const [timeline, setTimeline] = useState<TimelineBucket[]>([]);
  const [deploys, setDeploys] = useState<Deploy[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [incidents, setIncidents] = useState<any[]>([]);
  const [analyticsOverview, setAnalyticsOverview] = useState<any>(null);
  const [selectedDeploy, setSelectedDeploy] = useState<Deploy | null>(null);
  const [aiCost, setAiCost] = useState<AiCost | null>(null);
  const [selectedIssue, setSelectedIssue] = useState<Issue | null>(null);

  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

  // Initialize from localStorage or defaults
  useEffect(() => {
    setIsMounted(true);
    const savedKey = loadReadKey();
    const savedEp = loadEndpoint();
    setApiKey(savedKey);
    setEndpoint(savedEp);
  }, []);

  const saveSettings = (newKey: string, newEp: string) => {
    setApiKey(newKey);
    setEndpoint(newEp);
    saveReadKey(newKey);
    saveEndpoint(newEp);
  };

  const getApiUrl = (path: string) => {
    if (!endpoint) return path;
    const base = endpoint.trim().replace(/\/+$/, "");
    return `${base}${path}`;
  };


  const fetchData = async () => {
    if (!apiKey) {
      return;
    }
    setLoading(true);
    setErrorMsg(null);

    const headers = {
      "X-API-Key": apiKey.trim(),
      "Content-Type": "application/json",
    };

    try {
      // 1. Fetch Timeline
      const timelineRes = await fetch(getApiUrl(`/api/timeline?hours=${timeRangeHours}`), { headers });
      if (!timelineRes.ok) {
        throw new Error(`Failed to load timeline: HTTP ${timelineRes.status} ${timelineRes.statusText}`);
      }
      const timelineData = await timelineRes.json();
      setTimeline(Array.isArray(timelineData) ? timelineData : timelineData.timeline || []);

      // 2. Fetch Deploys
      const deploysRes = await fetch(getApiUrl("/api/deploys?limit=50"), { headers });
      if (!deploysRes.ok) {
        throw new Error(`Failed to load deploys: HTTP ${deploysRes.status} ${deploysRes.statusText}`);
      }
      const deploysData: Deploy[] = await deploysRes.json();
      setDeploys(deploysData);
      // Use the current selection, not the one this function was created with: the 15 s refresh
      // runs an older copy of fetchData and used to jump back to the newest deploy every time
      setSelectedDeploy((current) => {
        if (!current) return deploysData[0] ?? null;
        return deploysData.find((d) => d.id === current.id) ?? current;
      });

      // 3. Fetch Issues
      const issuesRes = await fetch(getApiUrl("/api/issues?limit=50"), { headers });
      if (!issuesRes.ok) {
        throw new Error(`Failed to load issues: HTTP ${issuesRes.status} ${issuesRes.statusText}`);
      }
      const issuesData: Issue[] = await issuesRes.json();
      setIssues(issuesData);

      // 4. Fetch Incidents
      try {
        const incRes = await fetch(getApiUrl("/api/incidents?limit=20"), { headers });
        if (incRes.ok) {
          const incData = await incRes.json();
          setIncidents(incData);
        }
      } catch (e) {
        console.warn("Failed to fetch incidents:", e);
      }

      // 5. Fetch Analytics Overview for KPIs
      try {
        const anaRes = await fetch(getApiUrl("/v1/analytics/overview?period=7"), { headers });
        if (anaRes.ok) {
          const anaData = await anaRes.json();
          setAnalyticsOverview(anaData);
        }
      } catch (e) {
        console.warn("Failed to fetch analytics:", e);
      }
    } catch (err: any) {
      console.error("Dashboard fetch error:", err);
      setErrorMsg(err.message || "Failed to load ChangeWatch data");
    } finally {
      setLoading(false);
    }
  };

  // The app's own AI spend before and after the selected deploy (SDK record_ai_usage)
  const selectedDeployId = selectedDeploy?.id;
  useEffect(() => {
    if (!apiKey || !selectedDeployId) return;
    let cancelled = false;
    fetch(getApiUrl(`/api/deploys/${selectedDeployId}/ai-cost`), { headers: { "X-API-Key": apiKey.trim() } })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled) setAiCost(data);
      })
      .catch(() => {
        if (!cancelled) setAiCost(null);
      });
    return () => {
      cancelled = true;
    };
  }, [apiKey, endpoint, selectedDeployId]);

  // Load issue detail when clicked
  const selectIssueWithDetails = async (issue: Issue) => {
    setSelectedIssue(issue);
    if (!apiKey) return;
    try {
      const res = await fetch(getApiUrl(`/api/issues/${issue.id}`), {
        headers: { "X-API-Key": apiKey.trim() },
      });
      if (res.ok) {
        const detail = await res.json();
        setSelectedIssue(detail);
      }
    } catch (e) {
      console.error("Failed to load issue detail:", e);
    }
  };

  useEffect(() => {
    if (apiKey) {
      fetchData();
    }
  }, [apiKey, endpoint, timeRangeHours]);

  useEffect(() => {
    if (!autoRefresh || !apiKey) return;
    const interval = setInterval(fetchData, 15000); // 15s refresh
    return () => clearInterval(interval);
  }, [autoRefresh, apiKey, endpoint, timeRangeHours]);

  // Aggregate summary metrics
  const metrics = useMemo(() => {
    const totalRequests = timeline.reduce((acc, b) => acc + (b.requests || 0), 0);
    const totalErrors = timeline.reduce((acc, b) => acc + (b.errors || 0), 0);
    // The rate uses only buckets with traffic counted: the newest one often has errors before its heartbeat
    const errorsWithTraffic = timeline.reduce((acc, b) => acc + ((b.requests || 0) > 0 ? b.errors || 0 : 0), 0);
    const rollbackCount = deploys.filter((d) => d.verdict === "rollback").length;
    const watchCount = deploys.filter((d) => d.verdict === "watch").length;
    return {
      totalRequests,
      totalErrors,
      errorRate: totalRequests > 0 ? ((errorsWithTraffic / totalRequests) * 100).toFixed(2) : "0.00",
      rollbackCount,
      watchCount,
    };
  }, [timeline, deploys]);

  const getVerdictBadge = (verdict: string) => {
    switch (verdict) {
      case "safe":
        return <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">SAFE</span>;
      case "watch":
        return <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">WATCH</span>;
      case "rollback":
        return <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/30">ROLLBACK</span>;
      default:
        return <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-500/20 text-slate-400 border border-slate-500/30">NO DATA</span>;
    }
  };

  // Format chart data
  const chartData = useMemo(() => {
    return timeline.map((bucket) => {
      const timeStr = formatTime(bucket.bucket);
      const hasDeploys = bucket.deploys && bucket.deploys.length > 0;
      return {
        time: timeStr,
        requests: bucket.requests,
        errors: bucket.errors,
        deployLabel: hasDeploys ? bucket.deploys.map((d) => d.commit_sha.slice(0, 7)).join(", ") : null,
      };
    });
  }, [timeline]);

  if (!isMounted) return null;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Top Navbar */}
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30 px-6 py-3.5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center space-x-3">
          <div className="h-8 w-8 rounded-lg bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/30">
            CW
          </div>
          <div>
            <h1 className="font-bold text-lg tracking-tight flex items-center gap-2">
              ChangeWatch
              <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 font-mono">
                v1.0
              </span>
            </h1>
            <p className="text-xs text-slate-400">Release Anomaly Detection & Timeline</p>
          </div>
        </div>

        {/* Global Navigation */}
        <nav className="flex flex-wrap items-center gap-2">
          <NavLinks openIncidents={incidents.filter((i) => i.status === "possible" || i.status === "confirmed").length} />
        </nav>

        {/* API Key & Controls */}
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center space-x-2 bg-slate-800/80 rounded-lg px-3 py-1.5 border border-slate-700">
            <span className="text-xs text-slate-400 font-medium">Read Key:</span>
            <input
              type="password"
              placeholder="cw_read_..."
              value={apiKey}
              onChange={(e) => saveSettings(e.target.value, endpoint)}
              className="bg-transparent text-xs text-slate-200 outline-none w-48 font-mono placeholder:text-slate-500"
            />
          </div>

          <div className="flex items-center space-x-2 bg-slate-800/80 rounded-lg px-3 py-1.5 border border-slate-700">
            <span className="text-xs text-slate-400 font-medium">API:</span>
            <input
              type="text"
              placeholder="http://localhost:8000"
              value={endpoint}
              onChange={(e) => saveSettings(apiKey, e.target.value)}
              className="bg-transparent text-xs text-slate-200 outline-none w-36 font-mono placeholder:text-slate-500"
            />
          </div>

          <select
            value={timeRangeHours}
            onChange={(e) => setTimeRangeHours(Number(e.target.value))}
            className="bg-slate-800 text-xs text-slate-200 border border-slate-700 rounded-lg px-3 py-1.5 outline-none font-medium"
          >
            <option value={6}>Last 6 Hours</option>
            <option value={12}>Last 12 Hours</option>
            <option value={24}>Last 24 Hours</option>
            <option value={48}>Last 48 Hours</option>
            <option value={168}>Last 7 Days</option>
          </select>

          <button
            onClick={fetchData}
            disabled={loading || !apiKey}
            className="px-3.5 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 disabled:text-slate-500 rounded-lg text-xs font-semibold shadow transition-colors flex items-center gap-1.5"
          >
            {loading ? "Refreshing..." : "Refresh"}
          </button>

          <label className="flex items-center space-x-1.5 text-xs text-slate-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded bg-slate-800 border-slate-700 text-indigo-600 focus:ring-0"
            />
            <span>Auto</span>
          </label>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 p-6 max-w-7xl w-full mx-auto space-y-6">
        {!apiKey && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-sm flex items-center justify-between">
            <div>
              <strong>Read Key Required:</strong> Enter your project&apos;s read key (<code className="bg-amber-950/50 px-1 py-0.5 rounded text-amber-300">cw_read_...</code>) in the top bar to connect.
            </div>
          </div>
        )}

        {errorMsg && (
          <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm">
            <strong>Error:</strong> {errorMsg}
          </div>
        )}

        {/* Active Incident Banner */}
        {incidents.filter((i) => i.status === "possible" || i.status === "confirmed").length > 0 && (() => {
          const activeInc = incidents.find((i) => i.status === "possible" || i.status === "confirmed");
          return (
            <div className="p-4 rounded-xl bg-rose-950/40 border border-rose-500/50 text-rose-200 flex flex-col sm:flex-row sm:items-center justify-between gap-3 shadow-lg shadow-rose-950/20">
              <div className="flex items-center gap-3">
                <span className="flex h-3 w-3 relative">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-3 w-3 bg-rose-500"></span>
                </span>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-sm text-white">Active Incident:</span>
                    <span className="font-semibold text-rose-300">{activeInc.title}</span>
                    <span className="text-[10px] px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/30 uppercase font-mono font-semibold">
                      {activeInc.status}
                    </span>
                  </div>
                  <div className="text-xs text-rose-300/80 mt-0.5">
                    Location: {activeInc.where_file}:{activeInc.where_line}
                    {activeInc.owners && activeInc.owners.length > 0 && ` • Owners: ${activeInc.owners.join(", ")}`}
                  </div>
                </div>
              </div>
              <Link
                href={`/incidents/${activeInc.id}`}
                className="px-3.5 py-1.5 bg-rose-600 hover:bg-rose-500 text-white rounded-lg text-xs font-semibold whitespace-nowrap transition self-start sm:self-auto shadow"
              >
                View Incident Details →
              </Link>
            </div>
          );
        })()}

        {/* Team Dashboard 4 KPIs */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Releases This Week</span>
            <div className="text-2xl font-bold mt-1 text-slate-100">
              {analyticsOverview?.kpis?.releases?.formatted ?? (deploys.length > 0 ? `${deploys.length}` : "Not enough data yet")}
            </div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Latest Release Verdict</span>
            <div className="mt-1 flex items-center gap-2">
              {getVerdictBadge(deploys.length > 0 ? deploys[0].verdict : "no_data")}
              {deploys.length > 0 && (
                <span className="text-xs font-mono text-slate-400">{deploys[0].commit_sha.slice(0, 7)}</span>
              )}
            </div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Median Time to Fix (TTF)</span>
            <div className="text-xl font-bold mt-1 text-emerald-400">
              {analyticsOverview?.kpis?.time_to_fix?.formatted ?? "Not enough data yet"}
            </div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Share of Real Alerts</span>
            <div className="text-xl font-bold mt-1 text-indigo-400">
              {analyticsOverview?.kpis?.alerts_real?.formatted ?? "Not enough data yet"}
            </div>
          </div>
        </div>

        {/* Traffic Metric Summary Cards */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Total Deploys Tracked</span>
            <div className="text-2xl font-bold mt-1 text-slate-100">{deploys.length}</div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Rollback / Watch Alerts</span>
            <div className="text-2xl font-bold mt-1 text-rose-400">
              {metrics.rollbackCount} <span className="text-xs font-normal text-slate-400">/ {metrics.watchCount} watch</span>
            </div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Traffic Requests ({timeRangeHours}h)</span>
            <div className="text-2xl font-bold mt-1 text-indigo-400">{metrics.totalRequests.toLocaleString()}</div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm">
            <span className="text-xs text-slate-400 font-medium">Error Rate</span>
            <div className="text-2xl font-bold mt-1 text-amber-400">
              {metrics.errorRate}% <span className="text-xs font-normal text-slate-400">({metrics.totalErrors} errors)</span>
            </div>
          </div>
        </div>

        {/* 5-Minute Bucketed Timeline Chart */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-slate-200">5-Minute Event & Traffic Timeline</h2>
              <p className="text-xs text-slate-400">Requests, error counts, and release deploys over the last {timeRangeHours} hours</p>
            </div>
            <div className="flex items-center gap-4 text-xs text-slate-400">
              <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-indigo-500"></span> Requests</span>
              <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-rose-500"></span> Errors</span>
            </div>
          </div>

          <div className="h-64 w-full">
            {chartData.length === 0 ? (
              <div className="h-full flex items-center justify-center text-xs text-slate-500">
                No traffic or error events recorded in this time window.
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={chartData} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="time" stroke="#64748b" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#64748b" tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{ backgroundColor: "#0f172a", borderColor: "#334155", borderRadius: "0.5rem", fontSize: "12px" }}
                    labelStyle={{ color: "#94a3b8" }}
                  />
                  <Area type="monotone" dataKey="requests" fill="#6366f1" fillOpacity={0.15} stroke="#6366f1" strokeWidth={2} name="Requests" />
                  <Bar dataKey="errors" fill="#ef4444" radius={[3, 3, 0, 0]} name="Errors" />
                </ComposedChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* Tabs: Deploys vs Issues */}
        <div className="flex items-center space-x-2 border-b border-slate-800">
          <button
            onClick={() => setActiveTab("deploys")}
            className={`px-4 py-2 text-sm font-semibold border-b-2 transition-colors ${
              activeTab === "deploys"
                ? "border-indigo-500 text-indigo-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            Release Deploys ({deploys.length})
          </button>
          <button
            onClick={() => setActiveTab("issues")}
            className={`px-4 py-2 text-sm font-semibold border-b-2 transition-colors ${
              activeTab === "issues"
                ? "border-indigo-500 text-indigo-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            Error Issues ({issues.length})
          </button>
        </div>

        {/* Tab 1: Deploy Inspection Panel */}
        {activeTab === "deploys" && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* Left Column: Deploy List */}
            <div className="lg:col-span-5 space-y-2">
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Recent Deploys</h3>
              <div className="space-y-2 max-h-[600px] overflow-y-auto pr-1">
                {deploys.length === 0 ? (
                  <div className="p-8 text-center text-slate-500 text-sm bg-slate-900 border border-slate-800 rounded-xl">
                    No deploys recorded yet.
                  </div>
                ) : (
                  deploys.map((dep) => {
                    const isSelected = selectedDeploy?.id === dep.id;
                    return (
                      <div
                        key={dep.id}
                        onClick={() => setSelectedDeploy(dep)}
                        className={`p-3.5 rounded-xl border cursor-pointer transition-all ${
                          isSelected
                            ? "bg-slate-900 border-indigo-500/80 shadow-md ring-1 ring-indigo-500/30"
                            : "bg-slate-900/60 border-slate-800 hover:border-slate-700"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-xs font-semibold text-slate-200">{dep.commit_sha.slice(0, 8)}</span>
                          {getVerdictBadge(dep.verdict)}
                        </div>
                        <p className="text-xs text-slate-300 mt-1 line-clamp-1">{dep.message || "No commit message"}</p>
                        <div className="flex items-center justify-between text-[11px] text-slate-500 mt-2">
                          <span>{dep.author || "Unknown"} • {dep.environment}</span>
                          <span>{formatTime(dep.deployed_at)}</span>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>

            {/* Right Column: Deploy Inspection Panel */}
            <div className="lg:col-span-7">
              {selectedDeploy ? (
                <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-5 shadow-sm">
                  {/* Verdict & Header */}
                  <div className="flex items-start justify-between border-b border-slate-800 pb-4">
                    <div>
                      <div className="flex items-center gap-3">
                        <span className="font-mono text-base font-bold text-slate-100">{selectedDeploy.commit_sha}</span>
                        {getVerdictBadge(selectedDeploy.verdict)}
                      </div>
                      <p className="text-xs text-slate-400 mt-1">
                        By {selectedDeploy.author || "Unknown"} in <span className="text-indigo-400">{selectedDeploy.environment}</span> on {formatDateTime(selectedDeploy.deployed_at)}
                      </p>
                    </div>
                  </div>

                  {/* Verdict Reasons */}
                  <div>
                    <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">Release Verdict Analysis</h4>
                    <div className="p-3 bg-slate-950/60 border border-slate-800 rounded-lg space-y-1">
                      {selectedDeploy.verdict_reasons && selectedDeploy.verdict_reasons.length > 0 ? (
                        selectedDeploy.verdict_reasons.map((reason, idx) => (
                          <div key={idx} className="text-xs text-slate-300 flex items-start gap-2">
                            <span className="text-indigo-400 mt-0.5">•</span>
                            <span>{reason}</span>
                          </div>
                        ))
                      ) : (
                        <span className="text-xs text-slate-500">No anomaly detected.</span>
                      )}
                    </div>
                  </div>

                  {/* AI Likely Cause (if available) */}
                  {selectedDeploy.likely_cause && (
                    <div className="p-3.5 bg-indigo-950/20 border border-indigo-500/30 rounded-xl space-y-1">
                      <div className="flex items-center gap-2 text-xs font-semibold text-indigo-300">
                        <span>✨ AI Root-Cause Explanation</span>
                      </div>
                      <p className="text-xs text-slate-200 leading-relaxed">{selectedDeploy.likely_cause}</p>
                    </div>
                  )}

                  {/* The app's AI spend around this deploy */}
                  {aiCost && aiCost.deploy_id === selectedDeploy.id && (aiCost.before.calls > 0 || aiCost.after.calls > 0) && (
                    <div className={`p-3.5 rounded-xl border space-y-1 ${aiCost.jumped ? "bg-amber-950/20 border-amber-500/40" : "bg-slate-950 border-slate-800"}`}>
                      <div className="text-xs font-semibold text-slate-300">
                        AI spend {aiCost.basis === "per_call" ? "per call" : "per 1,000 requests"}
                        {aiCost.change && <span className={aiCost.jumped ? "text-amber-300" : "text-slate-400"}> · {aiCost.change}</span>}
                      </div>
                      <p className="text-xs text-slate-300">
                        Before: {(aiCost.basis === "per_call" ? aiCost.before.per_call : aiCost.before.per_1k_requests) ?? "no data"} ·
                        After: {(aiCost.basis === "per_call" ? aiCost.after.per_call : aiCost.after.per_1k_requests) ?? "no data"} ·
                        {" "}{aiCost.after.cost} on {aiCost.after.calls} calls since the deploy
                        {aiCost.after.unpriced_calls > 0 && ` (${aiCost.after.unpriced_calls} calls to models without a price)`}
                      </p>
                      {aiCost.top.length > 0 && (
                        <p className="text-[11px] text-slate-500">
                          {aiCost.top.map((t) => `${t.model}${t.feature ? ` (${t.feature})` : ""}: ${t.cost}`).join(" · ")}
                        </p>
                      )}
                    </div>
                  )}

                  {/* Commit Message */}
                  {selectedDeploy.message && (
                    <div>
                      <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">Commit Message</h4>
                      <p className="text-xs text-slate-300 bg-slate-950/50 p-2.5 rounded-lg border border-slate-800 font-mono">
                        {selectedDeploy.message}
                      </p>
                    </div>
                  )}

                  {/* Git Diff Inspection */}
                  <div>
                    <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1">Deploy Diff</h4>
                    {selectedDeploy.diff ? (
                      <pre className="text-[11px] font-mono bg-slate-950 p-3 rounded-lg border border-slate-800 overflow-x-auto max-h-60 text-slate-300">
                        {selectedDeploy.diff}
                      </pre>
                    ) : (
                      <p className="text-xs text-slate-500 italic">No diff payload supplied for this deploy.</p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="p-12 text-center text-slate-500 text-sm bg-slate-900 border border-slate-800 rounded-xl">
                  Select a deploy from the left panel to inspect verdict details, AI cause analysis, and diff.
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tab 2: Issues List & Trace Inspection */}
        {activeTab === "issues" && (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
            {/* Left Column: Issues Table */}
            <div className="lg:col-span-6 space-y-2">
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Fingerprinted Issues</h3>
              <div className="space-y-2 max-h-[600px] overflow-y-auto pr-1">
                {issues.length === 0 ? (
                  <div className="p-8 text-center text-slate-500 text-sm bg-slate-900 border border-slate-800 rounded-xl">
                    No issues recorded yet.
                  </div>
                ) : (
                  issues.map((iss) => {
                    const isSelected = selectedIssue?.id === iss.id;
                    return (
                      <div
                        key={iss.id}
                        onClick={() => selectIssueWithDetails(iss)}
                        className={`p-3.5 rounded-xl border cursor-pointer transition-all ${
                          isSelected
                            ? "bg-slate-900 border-indigo-500/80 shadow-md ring-1 ring-indigo-500/30"
                            : "bg-slate-900/60 border-slate-800 hover:border-slate-700"
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-semibold text-slate-200 line-clamp-1">{iss.title}</span>
                          <span className="text-xs px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-300 font-mono">
                            {iss.event_count} events
                          </span>
                        </div>
                        <div className="flex items-center justify-between text-[11px] text-slate-500 mt-2">
                          <span className="font-mono text-slate-400">fp: {iss.fingerprint.slice(0, 10)}...</span>
                          <span>Last seen: {formatTime(iss.last_seen)}</span>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>

            {/* Right Column: Issue Stack Trace Inspector */}
            <div className="lg:col-span-6">
              {selectedIssue ? (
                <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-4 shadow-sm">
                  <div>
                    <h4 className="text-sm font-bold text-slate-100">{selectedIssue.title}</h4>
                    <p className="text-xs text-slate-400 mt-1">
                      Status: <span className="capitalize text-emerald-400">{selectedIssue.status}</span> • Total Events: {selectedIssue.event_count}
                    </p>
                  </div>

                  {/* Recent Events & Traces */}
                  <div>
                    <h5 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Recent Event Traces</h5>
                    {selectedIssue.events && selectedIssue.events.length > 0 ? (
                      <div className="space-y-3 max-h-[450px] overflow-y-auto">
                        {selectedIssue.events.map((ev) => (
                          <div key={ev.id} className="p-3 bg-slate-950 rounded-lg border border-slate-800 text-xs space-y-1.5">
                            <div className="flex items-center justify-between text-slate-400 text-[11px]">
                              <span>Path: <strong className="text-slate-200">{ev.request_path || "N/A"}</strong></span>
                              <span>{formatTime(ev.occurred_at)}</span>
                            </div>
                            {ev.stack_trace && (
                              <pre className="text-[11px] font-mono bg-slate-900 p-2.5 rounded text-rose-300 overflow-x-auto whitespace-pre-wrap">
                                {ev.stack_trace}
                              </pre>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-slate-500 italic">No events or traces available for this issue.</p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="p-12 text-center text-slate-500 text-sm bg-slate-900 border border-slate-800 rounded-xl">
                  Select an issue from the left panel to inspect error traces and recent occurrences.
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
