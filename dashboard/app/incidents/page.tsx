"use client";

import React, { useEffect, useState, useMemo } from "react";
import Link from "next/link";
import { loadEndpoint, loadReadKey } from "../lib/session";
import NavLinks from "../components/NavLinks";

interface Incident {
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
  commit_sha?: string;
  author?: string;
}

const formatDateTime = (d?: string | null): string => {
  if (!d) return "—";
  const date = new Date(d);
  return isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

const getStatusBadge = (status: string) => {
  switch (status) {
    case "confirmed":
      return (
        <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/30">
          CONFIRMED
        </span>
      );
    case "possible":
      return (
        <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-400 border border-amber-500/30">
          POSSIBLE
        </span>
      );
    case "resolved":
      return (
        <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
          RESOLVED
        </span>
      );
    case "dismissed":
      return (
        <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-500/20 text-slate-400 border border-slate-500/30">
          DISMISSED
        </span>
      );
    default:
      return (
        <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-500/20 text-slate-400 border border-slate-500/30">
          {status.toUpperCase()}
        </span>
      );
  }
};

export default function IncidentsPage() {
  const [apiKey, setApiKey] = useState<string>("");
  const [endpoint, setEndpoint] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<"all" | "open" | "resolved">("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    const savedKey = loadReadKey();
    const savedEp = loadEndpoint();
    setApiKey(savedKey);
    setEndpoint(savedEp);
  }, []);

  const getApiUrl = (path: string) => {
    if (!endpoint) return path;
    const base = endpoint.trim().replace(/\/+$/, "");
    return `${base}${path}`;
  };

  const fetchIncidents = async () => {
    if (!apiKey) return;
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(getApiUrl("/api/incidents?limit=100"), {
        headers: {
          "X-API-Key": apiKey.trim(),
          "Content-Type": "application/json",
        },
      });
      if (!res.ok) {
        throw new Error(`Failed to load incidents: HTTP ${res.status}`);
      }
      const data: Incident[] = await res.json();
      setIncidents(data);
    } catch (err: any) {
      console.error("Incidents fetch error:", err);
      setErrorMsg(err.message || "Failed to load incidents");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (apiKey) {
      fetchIncidents();
    }
  }, [apiKey, endpoint]);

  const filteredIncidents = useMemo(() => {
    return incidents.filter((inc) => {
      if (statusFilter === "open" && inc.status !== "possible" && inc.status !== "confirmed") {
        return false;
      }
      if (statusFilter === "resolved" && inc.status !== "resolved" && inc.status !== "dismissed") {
        return false;
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchTitle = inc.title.toLowerCase().includes(q);
        const matchFile = inc.where_file.toLowerCase().includes(q);
        const matchCategory = inc.category?.toLowerCase().includes(q);
        const matchOwners = inc.owners?.some((o) => o.toLowerCase().includes(q));
        if (!matchTitle && !matchFile && !matchCategory && !matchOwners) return false;
      }
      return true;
    });
  }, [incidents, statusFilter, searchQuery]);

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
            <p className="text-xs text-slate-400">Release Incidents & SRE Triage</p>
          </div>
        </div>

        {/* Global Navigation */}
        <nav className="flex flex-wrap items-center gap-2">
          <NavLinks />
        </nav>

        {/* API Key info */}
        <div className="flex items-center space-x-3">
          <button
            onClick={fetchIncidents}
            disabled={loading || !apiKey}
            className="px-3.5 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 disabled:text-slate-500 rounded-lg text-xs font-semibold shadow transition-colors"
          >
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 p-6 max-w-7xl w-full mx-auto space-y-6">
        {!apiKey && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-200 text-sm">
            <strong>Read Key Required:</strong> Please configure your read key on the{" "}
            <Link href="/" className="underline text-amber-300">
              Overview page
            </Link>{" "}
            to view incidents.
          </div>
        )}

        {errorMsg && (
          <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm">
            <strong>Error:</strong> {errorMsg}
          </div>
        )}

        {/* Filter & Controls Bar */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-4 bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-400 font-medium">Status:</span>
            <button
              onClick={() => setStatusFilter("all")}
              className={`px-3 py-1 text-xs rounded-lg font-medium transition ${
                statusFilter === "all"
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              All ({incidents.length})
            </button>
            <button
              onClick={() => setStatusFilter("open")}
              className={`px-3 py-1 text-xs rounded-lg font-medium transition ${
                statusFilter === "open"
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              Active / Open ({incidents.filter((i) => i.status === "possible" || i.status === "confirmed").length})
            </button>
            <button
              onClick={() => setStatusFilter("resolved")}
              className={`px-3 py-1 text-xs rounded-lg font-medium transition ${
                statusFilter === "resolved"
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              Resolved ({incidents.filter((i) => i.status === "resolved" || i.status === "dismissed").length})
            </button>
          </div>

          <div className="w-full sm:w-72">
            <input
              type="text"
              placeholder="Search by title, file, owner..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-slate-800/80 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-500 focus:border-indigo-500"
            />
          </div>
        </div>

        {/* Incidents Table */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs text-slate-300">
              <thead className="bg-slate-800/60 text-slate-400 uppercase font-semibold text-[11px] border-b border-slate-800">
                <tr>
                  <th className="py-3 px-4">Status</th>
                  <th className="py-3 px-4">Title / Error</th>
                  <th className="py-3 px-4">Location</th>
                  <th className="py-3 px-4">Owners</th>
                  <th className="py-3 px-4">Category</th>
                  <th className="py-3 px-4">Events</th>
                  <th className="py-3 px-4">First Alert</th>
                  <th className="py-3 px-4">Detected</th>
                  <th className="py-3 px-4 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {filteredIncidents.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="py-12 text-center text-slate-500">
                      {loading ? "Loading incidents..." : "No incidents found matching the filter criteria."}
                    </td>
                  </tr>
                ) : (
                  filteredIncidents.map((incident) => (
                    <tr
                      key={incident.id}
                      className="hover:bg-slate-800/40 transition group cursor-pointer"
                    >
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        {getStatusBadge(incident.status)}
                      </td>
                      <td className="py-3.5 px-4 font-medium text-slate-200">
                        <Link
                          href={`/incidents/${incident.id}`}
                          className="hover:text-indigo-400 transition"
                        >
                          {incident.title}
                        </Link>
                      </td>
                      <td className="py-3.5 px-4 font-mono text-slate-400 whitespace-nowrap">
                        <span>
                          {incident.where_file}:{incident.where_line}
                        </span>
                        {incident.in_diff && (
                          <span className="ml-1.5 px-1.5 py-0.2 rounded bg-indigo-900/60 text-indigo-300 border border-indigo-700/50 text-[10px]">
                            in diff
                          </span>
                        )}
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        <div className="flex flex-wrap gap-1">
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
                            <span className="text-slate-500">—</span>
                          )}
                        </div>
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        <span className="px-2 py-0.5 rounded bg-slate-800/80 text-slate-400 border border-slate-700/60 text-[11px]">
                          {incident.category || "unknown"}
                        </span>
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap font-bold text-slate-200">
                        {incident.event_count}
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap text-slate-400">
                        {incident.first_alert_ms !== null ? (
                          <span className="text-emerald-400 font-mono">
                            {incident.first_alert_ms} ms
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap text-slate-400">
                        {formatDateTime(incident.created_at)}
                      </td>
                      <td className="py-3.5 px-4 whitespace-nowrap text-right">
                        <Link
                          href={`/incidents/${incident.id}`}
                          className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-indigo-300 text-xs font-medium border border-slate-700 transition"
                        >
                          View Details →
                        </Link>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  );
}
