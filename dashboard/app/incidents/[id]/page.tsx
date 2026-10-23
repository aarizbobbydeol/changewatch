"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

interface TimelineEvent {
  event: string;
  timestamp: string;
  detail?: string;
}

interface IncidentDetail {
  id: number;
  deploy_id: number;
  issue_id: number;
  status: "possible" | "confirmed" | "resolved" | "dismissed";
  title: string;
  where_file: string;
  where_line: number;
  where_function: string | null;
  in_diff: boolean;
  owners: string[];
  event_count: number;
  category: string;
  first_alert_ms: number | null;
  created_at: string;
  confirmed_at: string | null;
  resolved_at: string | null;
  commit_sha: string | null;
  commit_author: string | null;
  commit_message: string | null;
  deployed_at: string | null;
  code_excerpt: string | null;
  timeline: TimelineEvent[];
  ai_investigation?: {
    status: string;
    summary?: string;
    hypothesis?: string;
    suggested_fix?: string;
  };
}

const formatDateTime = (d?: string | null): string => {
  if (!d) return "—";
  const date = new Date(d);
  return isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

export default function IncidentDetailPage() {
  const params = useParams();
  const incidentId = params?.id;

  const [apiKey, setApiKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [incident, setIncident] = useState<IncidentDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [actionStatus, setActionStatus] = useState<string | null>(null);

  useEffect(() => {
    const savedKey = localStorage.getItem("cw_read_key") || "";
    const savedEp = localStorage.getItem("cw_endpoint") || "";
    setApiKey(savedKey);
    setEndpoint(savedEp);
  }, []);

  const getApiUrl = (path: string) => {
    if (!endpoint) return path;
    const base = endpoint.trim().replace(/\/+$/, "");
    return `${base}${path}`;
  };

  const fetchIncident = async () => {
    if (!apiKey || !incidentId) return;
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(getApiUrl(`/api/incidents/${incidentId}`), {
        headers: {
          "X-API-Key": apiKey.trim(),
          "Content-Type": "application/json",
        },
      });
      if (!res.ok) {
        throw new Error(`Failed to load incident #${incidentId}: HTTP ${res.status}`);
      }
      const data: IncidentDetail = await res.json();
      setIncident(data);
    } catch (err: any) {
      console.error("Incident detail error:", err);
      setErrorMsg(err.message || "Failed to load incident details");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (apiKey && incidentId) {
      fetchIncident();
    }
  }, [apiKey, endpoint, incidentId]);

  const handleUpdateStatus = async (newStatus: "confirmed" | "resolved" | "dismissed") => {
    if (!apiKey || !incident) return;
    setActionStatus("Updating...");
    try {
      // Optimistic local update
      setIncident((prev) => (prev ? { ...prev, status: newStatus } : null));
      setActionStatus(`Status updated to ${newStatus}`);
      setTimeout(() => setActionStatus(null), 3000);
    } catch (e: any) {
      setActionStatus(`Error: ${e.message}`);
    }
  };

  const renderCodeExcerpt = () => {
    if (!incident?.code_excerpt) {
      return (
        <div className="p-4 text-xs text-slate-500 italic bg-slate-900 rounded-lg border border-slate-800">
          Source code excerpt not available for this deploy.
        </div>
      );
    }

    const lines = incident.code_excerpt.split("\n");
    // Center line highlight around where_line
    const startLine = Math.max(1, incident.where_line - Math.floor(lines.length / 2));

    return (
      <div className="bg-slate-900 border border-slate-800 rounded-lg overflow-hidden font-mono text-xs">
        <div className="bg-slate-800/80 px-4 py-2 border-b border-slate-700/80 flex items-center justify-between text-slate-300">
          <span className="font-semibold">{incident.where_file}</span>
          <span className="text-[11px] text-slate-400">Target Line: {incident.where_line}</span>
        </div>
        <div className="py-2 overflow-x-auto">
          {lines.map((line, idx) => {
            const currentLineNum = startLine + idx;
            const isTarget = currentLineNum === incident.where_line;
            return (
              <div
                key={idx}
                className={`flex items-center px-4 py-0.5 ${
                  isTarget ? "bg-rose-500/20 text-rose-200 border-l-2 border-rose-500 font-semibold" : "text-slate-300 hover:bg-slate-800/30"
                }`}
              >
                <span className={`w-10 select-none text-right pr-4 text-[11px] ${isTarget ? "text-rose-400 font-bold" : "text-slate-600"}`}>
                  {currentLineNum}
                </span>
                <pre className="flex-1 font-mono">{line || " "}</pre>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* Top Navbar */}
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30 px-6 py-3.5 flex items-center justify-between">
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
            <p className="text-xs text-slate-400">Incident Details & Triage</p>
          </div>
        </div>

        {/* Global Navigation */}
        <nav className="hidden lg:flex items-center gap-2">
          <Link
            href="/"
            className="px-3 py-1.5 text-xs rounded bg-slate-800/80 hover:bg-slate-800 text-slate-300 border border-slate-700 transition"
          >
            Releases & Timeline
          </Link>
          <Link
            href="/incidents"
            className="px-3 py-1.5 text-xs rounded bg-indigo-950 text-indigo-300 border border-indigo-700/50 font-medium transition"
          >
            Incidents
          </Link>
          <Link
            href="/analytics"
            className="px-3 py-1.5 text-xs rounded bg-slate-800/80 hover:bg-slate-800 text-slate-300 border border-slate-700 transition"
          >
            Analytics
          </Link>
          <Link
            href="/usage"
            className="px-3 py-1.5 text-xs rounded bg-slate-800/80 hover:bg-slate-800 text-slate-300 border border-slate-700 transition"
          >
            Usage & Budgets
          </Link>
        </nav>

        <div className="flex items-center space-x-3">
          <Link
            href="/incidents"
            className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 border border-slate-700 transition"
          >
            ← Back to Incidents
          </Link>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 p-6 max-w-7xl w-full mx-auto space-y-6">
        {loading && !incident && (
          <div className="p-8 text-center text-slate-400">Loading incident #{incidentId}...</div>
        )}

        {errorMsg && (
          <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm">
            <strong>Error:</strong> {errorMsg}
          </div>
        )}

        {incident && (
          <>
            {/* Header / Title Banner */}
            <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div className="space-y-1">
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-mono text-slate-500">Incident #{incident.id}</span>
                    <span
                      className={`px-2.5 py-0.5 rounded-full text-xs font-semibold uppercase border ${
                        incident.status === "confirmed"
                          ? "bg-rose-500/20 text-rose-400 border-rose-500/30"
                          : incident.status === "possible"
                          ? "bg-amber-500/20 text-amber-400 border-amber-500/30"
                          : incident.status === "resolved"
                          ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                          : "bg-slate-500/20 text-slate-400 border-slate-500/30"
                      }`}
                    >
                      {incident.status}
                    </span>
                    <span className="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 text-xs">
                      {incident.category || "anomaly"}
                    </span>
                  </div>
                  <h2 className="text-xl font-bold text-slate-100">{incident.title}</h2>
                </div>

                {/* Status action buttons */}
                <div className="flex items-center gap-2">
                  {incident.status === "possible" && (
                    <button
                      onClick={() => handleUpdateStatus("confirmed")}
                      className="px-3.5 py-1.5 bg-rose-600 hover:bg-rose-500 text-white rounded-lg text-xs font-semibold transition"
                    >
                      Confirm Incident
                    </button>
                  )}
                  {incident.status !== "resolved" && (
                    <button
                      onClick={() => handleUpdateStatus("resolved")}
                      className="px-3.5 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-xs font-semibold transition"
                    >
                      Resolve Incident
                    </button>
                  )}
                  {incident.status !== "dismissed" && (
                    <button
                      onClick={() => handleUpdateStatus("dismissed")}
                      className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg text-xs font-medium transition"
                    >
                      Dismiss
                    </button>
                  )}
                </div>
              </div>

              {actionStatus && (
                <div className="text-xs text-indigo-400 bg-indigo-950/40 border border-indigo-800/50 p-2 rounded-lg">
                  {actionStatus}
                </div>
              )}

              {/* Meta Grid */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-4 border-t border-slate-800 text-xs">
                <div>
                  <span className="text-slate-500 block">Deploy Commit</span>
                  <span className="font-mono text-slate-300 font-medium">
                    {incident.commit_sha ? incident.commit_sha.slice(0, 7) : "—"}
                  </span>
                  {incident.commit_author && (
                    <span className="text-slate-500 block text-[11px]">by {incident.commit_author}</span>
                  )}
                </div>
                <div>
                  <span className="text-slate-500 block">Owners</span>
                  <div className="flex flex-wrap gap-1 mt-0.5">
                    {incident.owners && incident.owners.length > 0 ? (
                      incident.owners.map((owner, idx) => (
                        <span
                          key={idx}
                          className="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 text-[10px]"
                        >
                          {owner}
                        </span>
                      ))
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </div>
                </div>
                <div>
                  <span className="text-slate-500 block">First Alert Latency</span>
                  <span className="text-emerald-400 font-mono font-medium">
                    {incident.first_alert_ms !== null ? `${incident.first_alert_ms} ms` : "—"}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">Event Impact</span>
                  <span className="text-slate-200 font-bold">{incident.event_count} events</span>
                </div>
              </div>
            </div>

            {/* Code Excerpt & Diff Location */}
            <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-200">Innermost Code Frame</h3>
                {incident.in_diff && (
                  <span className="px-2 py-0.5 rounded bg-indigo-900/50 text-indigo-300 border border-indigo-700/50 text-xs">
                    Introduced in Commit Diff
                  </span>
                )}
              </div>
              {renderCodeExcerpt()}
            </div>

            {/* Two-Column: Timeline & AI Investigation */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* Timeline (col 5) */}
              <div className="lg:col-span-5 bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4">
                <h3 className="text-sm font-semibold text-slate-200">Incident Timeline</h3>
                <div className="space-y-3 relative before:absolute before:left-2 before:top-2 before:bottom-2 before:w-0.5 before:bg-slate-800">
                  {incident.timeline && incident.timeline.length > 0 ? (
                    incident.timeline.map((item, idx) => (
                      <div key={idx} className="flex items-start pl-6 relative">
                        <span className="absolute left-1 top-1.5 w-2.5 h-2.5 rounded-full bg-indigo-500 ring-4 ring-slate-900"></span>
                        <div className="space-y-0.5">
                          <span className="text-xs font-semibold text-slate-200 capitalize">
                            {item.event.replace(/_/g, " ")}
                          </span>
                          <span className="text-[11px] text-slate-400 block font-mono">
                            {formatDateTime(item.timestamp)}
                          </span>
                          {item.detail && <p className="text-xs text-slate-400">{item.detail}</p>}
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="text-xs text-slate-500 pl-6">No timeline events recorded.</div>
                  )}
                </div>
              </div>

              {/* AI SRE Investigation (col 7) */}
              <div className="lg:col-span-7 bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-200">AI Root-Cause Investigation</h3>
                  <span className="px-2 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700 text-xs">
                    {incident.ai_investigation?.status === "completed"
                      ? "Completed"
                      : "Not enabled"}
                  </span>
                </div>

                {incident.ai_investigation?.status === "completed" ? (
                  <div className="space-y-3 text-xs">
                    <div>
                      <span className="text-slate-400 font-medium block">Summary</span>
                      <p className="text-slate-200 mt-1">{incident.ai_investigation.summary}</p>
                    </div>
                    {incident.ai_investigation.hypothesis && (
                      <div>
                        <span className="text-slate-400 font-medium block">Hypothesis</span>
                        <p className="text-slate-300 mt-1">{incident.ai_investigation.hypothesis}</p>
                      </div>
                    )}
                    {incident.ai_investigation.suggested_fix && (
                      <div>
                        <span className="text-slate-400 font-medium block">Suggested Fix</span>
                        <pre className="bg-slate-950 p-2 rounded border border-slate-800 text-slate-300 mt-1 font-mono">
                          {incident.ai_investigation.suggested_fix}
                        </pre>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="p-5 rounded-lg bg-slate-950 border border-slate-800/80 text-center space-y-2">
                    <div className="text-slate-300 text-xs font-semibold">
                      Autonomous Investigation Not Enabled
                    </div>
                    <p className="text-xs text-slate-500 max-w-md mx-auto">
                      ChangeWatch AI SRE can autonomously clone the repository at the deploy commit,
                      inspect diffs and dependency updates, and formulate a verifiable root cause
                      within minutes of release anomaly detection.
                    </p>
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
