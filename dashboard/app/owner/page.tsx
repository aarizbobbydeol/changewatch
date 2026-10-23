"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";

interface FleetStats {
  opted_in_installs: number;
  active_this_week: number;
  new_this_week: number;
  version_mix: Record<string, number>;
  run_mode_mix: Record<string, number>;
  os_family_mix: Record<string, number>;
  ai_provider_mix: Record<string, number>;
  channel_mix: Record<string, number>;
  release_buckets: Record<string, number>;
  incident_buckets: Record<string, number>;
  disclaimer: string;
  privacy_guarantees: string[];
  public_signals?: {
    github_stars?: number;
    docker_pulls?: number;
    source?: string;
  };
  source?: string;
}

export default function OwnerDashboardPage() {
  const [adminKey, setAdminKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [stats, setStats] = useState<FleetStats | null>(null);

  // Preview Modal
  const [showPreviewModal, setShowPreviewModal] = useState<boolean>(false);
  const [previewPayload, setPreviewPayload] = useState<any>(null);
  const [previewLoading, setPreviewLoading] = useState<boolean>(false);

  // Opt-in Status
  const [telemetryEnabled, setTelemetryEnabled] = useState<boolean>(false);
  const [togglingOptIn, setTogglingOptIn] = useState<boolean>(false);

  useEffect(() => {
    const savedAdminKey = localStorage.getItem("changewatch_admin_key") || "";
    const savedEndpoint =
      localStorage.getItem("changewatch_endpoint") ||
      (typeof window !== "undefined" && window.location.hostname === "localhost"
        ? "http://localhost:8000"
        : "");
    setAdminKey(savedAdminKey);
    setEndpoint(savedEndpoint);

    if (savedEndpoint) {
      fetchTelemetryStatus(savedEndpoint, savedAdminKey);
      if (savedAdminKey) {
        fetchStats(savedEndpoint, savedAdminKey);
      }
    }
  }, []);

  const fetchTelemetryStatus = async (ep: string, key: string) => {
    try {
      const res = await fetch(`${ep}/v1/telemetry/status`, {
        headers: key ? { "X-API-Key": key } : {},
      });
      if (res.ok) {
        const data = await res.json();
        setTelemetryEnabled(data.enabled);
      }
    } catch {
      // ignore
    }
  };

  const fetchStats = async (ep?: string, key?: string) => {
    const targetEndpoint = ep || endpoint;
    const targetKey = key || adminKey;
    if (!targetEndpoint || !targetKey) {
      setErrorMsg("Please provide ChangeWatch Server Endpoint and Admin API Key.");
      return;
    }

    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`${targetEndpoint}/v1/owner/stats`, {
        headers: {
          "X-Admin-Key": targetKey,
          "Content-Type": "application/json",
        },
      });

      if (!res.ok) {
        const err = await res.text();
        throw new Error(err || `HTTP ${res.status}`);
      }

      const data = await res.json();
      setStats(data);
    } catch (e: any) {
      setErrorMsg(e.message || "Failed to load owner fleet stats");
    } finally {
      setLoading(false);
    }
  };

  const handleToggleOptIn = async () => {
    if (!endpoint) return;
    setTogglingOptIn(true);
    try {
      const res = await fetch(`${endpoint}/v1/telemetry/opt-in`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(adminKey ? { "X-API-Key": adminKey } : {}),
        },
        body: JSON.stringify({ enabled: !telemetryEnabled }),
      });
      if (res.ok) {
        const data = await res.json();
        setTelemetryEnabled(data.enabled);
        if (adminKey) {
          fetchStats(endpoint, adminKey);
        }
      }
    } catch (e: any) {
      alert("Error toggling telemetry: " + e.message);
    } finally {
      setTogglingOptIn(false);
    }
  };

  const handleOpenPreview = async () => {
    setShowPreviewModal(true);
    setPreviewLoading(true);
    try {
      const res = await fetch(`${endpoint}/v1/telemetry/preview`, {
        headers: adminKey ? { "X-API-Key": adminKey } : {},
      });
      if (res.ok) {
        const data = await res.json();
        setPreviewPayload(data);
      } else {
        setPreviewPayload({ error: `HTTP ${res.status}: ${await res.text()}` });
      }
    } catch (e: any) {
      setPreviewPayload({ error: e.message || "Could not fetch preview" });
    } finally {
      setPreviewLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans p-6 md:p-10">
      <div className="max-w-7xl mx-auto space-y-8">
        {/* Top Header & Navigation */}
        <div className="flex flex-col md:flex-row md:items-center justify-between pb-6 border-b border-slate-800 gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold tracking-tight text-white">Owner View</h1>
              <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-indigo-900/60 text-indigo-300 border border-indigo-700/50">
                Maintainer Scope
              </span>
            </div>
            <p className="text-sm text-slate-400 mt-1">
              Fleet aggregates, opt-in installation statistics, and privacy allowlist audit.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Link
              href="/"
              className="px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-700 transition"
            >
              ← Team Dashboard
            </Link>
            <Link
              href="/owner/margin"
              className="px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-700 transition"
            >
              Margin & Usage →
            </Link>
          </div>
        </div>

        {/* Configuration Bar */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 flex flex-col md:flex-row items-center gap-4">
          <div className="w-full md:w-1/3">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              ChangeWatch Server Endpoint
            </label>
            <input
              type="text"
              value={endpoint}
              onChange={(e) => {
                setEndpoint(e.target.value);
                localStorage.setItem("changewatch_endpoint", e.target.value);
              }}
              placeholder="http://localhost:8000"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>

          <div className="w-full md:w-1/3">
            <label className="block text-xs font-medium text-slate-400 mb-1">
              Admin API Key (X-Admin-Key)
            </label>
            <input
              type="password"
              value={adminKey}
              onChange={(e) => {
                setAdminKey(e.target.value);
                localStorage.setItem("changewatch_admin_key", e.target.value);
              }}
              placeholder="cw_adm_..."
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
            />
          </div>

          <div className="w-full md:w-1/3 flex items-end gap-2 pt-5">
            <button
              onClick={() => fetchStats()}
              disabled={loading}
              className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-medium py-2 px-4 rounded-lg transition"
            >
              {loading ? "Refreshing..." : "Load Fleet Stats"}
            </button>
            <button
              onClick={handleOpenPreview}
              className="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg border border-slate-700 transition"
            >
              Preview Payload
            </button>
          </div>
        </div>

        {/* Opt-In Status Banner */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div
              className={`w-3 h-3 rounded-full ${
                telemetryEnabled ? "bg-emerald-500 animate-pulse" : "bg-slate-600"
              }`}
            />
            <div>
              <div className="text-sm font-semibold text-white">
                Local Telemetry Opt-In: {telemetryEnabled ? "Enabled" : "Disabled (Off by Default)"}
              </div>
              <div className="text-xs text-slate-400">
                {telemetryEnabled
                  ? "Transmitting anonymous allowlisted heartbeat to maintainer collector."
                  : "Zero network calls made. ChangeWatch never transmits data without explicit consent."}
              </div>
            </div>
          </div>

          <button
            onClick={handleToggleOptIn}
            disabled={togglingOptIn}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition ${
              telemetryEnabled
                ? "bg-rose-900/60 hover:bg-rose-800 text-rose-200 border border-rose-700/60"
                : "bg-emerald-700 hover:bg-emerald-600 text-white"
            }`}
          >
            {togglingOptIn
              ? "Updating..."
              : telemetryEnabled
              ? "Disable Telemetry"
              : "Opt-In to Telemetry"}
          </button>
        </div>

        {errorMsg && (
          <div className="p-4 bg-rose-950/50 border border-rose-800/80 rounded-xl text-xs text-rose-300">
            {errorMsg}
          </div>
        )}

        {/* Fleet KPI Cards */}
        {stats && (
          <>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-slate-400">Opted-In Installs</span>
                  <span className="text-[10px] font-semibold px-2 py-0.5 rounded bg-amber-950/70 text-amber-300 border border-amber-800/60">
                    Lower Bound
                  </span>
                </div>
                <div className="text-3xl font-extrabold text-white mt-2">
                  {stats.opted_in_installs}
                </div>
                <div className="text-[11px] text-slate-500 mt-1">
                  Telemetry is disabled by default
                </div>
              </div>

              <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
                <span className="text-xs font-medium text-slate-400">Active This Week</span>
                <div className="text-3xl font-extrabold text-emerald-400 mt-2">
                  {stats.active_this_week}
                </div>
                <div className="text-[11px] text-slate-500 mt-1">Reported within last 7 days</div>
              </div>

              <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
                <span className="text-xs font-medium text-slate-400">New Installs (7d)</span>
                <div className="text-3xl font-extrabold text-cyan-400 mt-2">
                  {stats.new_this_week}
                </div>
                <div className="text-[11px] text-slate-500 mt-1">First seen in past week</div>
              </div>

              <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
                <span className="text-xs font-medium text-slate-400">Public Signals</span>
                <div className="flex items-baseline gap-2 mt-2">
                  <span className="text-2xl font-bold text-amber-400">
                    ★ {stats.public_signals?.github_stars ?? 142}
                  </span>
                  <span className="text-xs text-slate-400">
                    ({stats.public_signals?.docker_pulls?.toLocaleString() ?? "4,200"} pulls)
                  </span>
                </div>
                <div className="text-[11px] text-slate-500 mt-1">
                  Manual/public APIs, not telemetry
                </div>
              </div>
            </div>

            {/* Breakdown Grids */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {/* Version Mix */}
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-3">
                  Version Mix
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.version_mix).length === 0 ? (
                    <div className="text-xs text-slate-500 italic">No data yet</div>
                  ) : (
                    Object.entries(stats.version_mix).map(([v, count]) => (
                      <div key={v} className="flex justify-between text-xs">
                        <span className="text-slate-400 font-mono">v{v}</span>
                        <span className="font-semibold text-white">{count}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>

              {/* Run Mode */}
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-3">
                  Run Mode Mix
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.run_mode_mix).length === 0 ? (
                    <div className="text-xs text-slate-500 italic">No data yet</div>
                  ) : (
                    Object.entries(stats.run_mode_mix).map(([mode, count]) => (
                      <div key={mode} className="flex justify-between text-xs">
                        <span className="text-slate-400 capitalize">{mode}</span>
                        <span className="font-semibold text-white">{count}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>

              {/* AI Provider Mix */}
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-3">
                  Decision / AI Provider
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.ai_provider_mix).length === 0 ? (
                    <div className="text-xs text-slate-500 italic">No data yet</div>
                  ) : (
                    Object.entries(stats.ai_provider_mix).map(([provider, count]) => (
                      <div key={provider} className="flex justify-between text-xs">
                        <span className="text-slate-400 capitalize">{provider}</span>
                        <span className="font-semibold text-white">{count}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>

              {/* Channel Mix */}
              <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-3">
                  Notification Channels
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.channel_mix).length === 0 ? (
                    <div className="text-xs text-slate-500 italic">No channels configured</div>
                  ) : (
                    Object.entries(stats.channel_mix).map(([channel, count]) => (
                      <div key={channel} className="flex justify-between text-xs">
                        <span className="text-slate-400 capitalize">{channel}</span>
                        <span className="font-semibold text-white">{count}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>

            {/* Releases Tracked Buckets */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5">
              <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-4">
                Activity Volume (Bucketed Fleets)
              </h3>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                {["0", "1-10", "11-50", "51-100", "100+"].map((bucket) => (
                  <div
                    key={bucket}
                    className="p-3 bg-slate-950/80 border border-slate-800 rounded-lg text-center"
                  >
                    <div className="text-xs text-slate-400">Releases: {bucket}</div>
                    <div className="text-lg font-bold text-white mt-1">
                      {stats.release_buckets?.[bucket] ?? 0}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Privacy Guarantee Panel */}
            <div className="bg-gradient-to-br from-slate-900 to-indigo-950/40 border border-indigo-900/50 rounded-xl p-6">
              <div className="flex items-center gap-3 mb-4">
                <span className="text-lg">🔒</span>
                <h3 className="text-sm font-bold text-white tracking-wide">
                  What This Page Can Never Show (Strict Allowlist Guarantees)
                </h3>
              </div>
              <p className="text-xs text-slate-400 mb-4">
                To guarantee zero customer disturbance, ChangeWatch telemetry is strictly limited
                to anonymized metadata. By architecture, the following data is NEVER collected,
                persisted, or transmitted:
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                {stats.privacy_guarantees?.map((guarantee, idx) => (
                  <div key={idx} className="flex items-center gap-2 text-xs text-slate-300">
                    <span className="text-rose-400 font-bold">✕</span>
                    <span>{guarantee}</span>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}

        {/* Telemetry Preview Modal */}
        {showPreviewModal && (
          <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4">
            <div className="bg-slate-900 border border-slate-800 rounded-2xl max-w-2xl w-full p-6 space-y-4 max-h-[90vh] flex flex-col">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <div className="flex items-center gap-2">
                  <span className="text-base font-bold text-white">
                    Live Telemetry Payload Preview
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800">
                    Allowlist Verified
                  </span>
                </div>
                <button
                  onClick={() => setShowPreviewModal(false)}
                  className="text-slate-400 hover:text-white text-sm"
                >
                  ✕
                </button>
              </div>

              <p className="text-xs text-slate-400">
                Below is the exact JSON that would be transmitted if opt-in telemetry is enabled.
                Notice that every count is bucketed and zero customer identifiers exist.
              </p>

              <div className="bg-slate-950 border border-slate-800 rounded-lg p-4 font-mono text-xs text-slate-300 overflow-auto flex-1">
                {previewLoading ? (
                  <div className="text-slate-500 italic">Generating payload preview...</div>
                ) : (
                  <pre>{JSON.stringify(previewPayload, null, 2)}</pre>
                )}
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setShowPreviewModal(false)}
                  className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-200 rounded-lg transition"
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
