"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import NavLinks from "../components/NavLinks";
import { apiUrl, changeHeaders, loadEndpoint, loadManageKey, loadReadKey, saveEndpoint, saveManageKey, saveReadKey } from "../lib/session";
import { dollarsToMicros, microsToDollars } from "../lib/money";

interface Channel {
  id: number;
  type: string;
  enabled: boolean;
}

interface Member {
  id: number;
  name: string;
  git_emails: string[] | null;
  slack_user_id: string | null;
  discord_user_id: string | null;
  email: string | null;
}

interface ProjectSettings {
  id: number;
  name: string;
  critical_routes: string[];
  notify_quiet: boolean;
  failed_request_value_micros: number | null;
  channels: Channel[];
  team_members: Member[];
}

interface Runbook {
  id: number;
  title: string;
  steps: string;
  match_exception: string | null;
  match_path: string | null;
  match_route: string | null;
  url: string | null;
}

const EMPTY_RUNBOOK = { title: "", steps: "", match_exception: "", match_path: "", match_route: "", url: "" };

// FastAPI answers 422 with a list of {msg}; anything else with {detail: "..."}
function apiError(body: any, status: number): string {
  const detail = body?.detail;
  if (Array.isArray(detail)) return detail.map((d: any) => String(d.msg || "").replace(/^Value error, /, "")).join("; ");
  return typeof detail === "string" ? detail : `HTTP ${status}`;
}

const CHANNEL_LABELS: Record<string, string> = {
  slack: "Slack",
  discord: "Discord",
  teams: "Microsoft Teams",
  email: "Email",
  webhook: "Signed webhook",
};

export default function SettingsPage() {
  const [readKey, setReadKey] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [draftKey, setDraftKey] = useState("");
  const [draftEndpoint, setDraftEndpoint] = useState("");
  const [draftManage, setDraftManage] = useState("");
  const [hasManage, setHasManage] = useState(false);
  const [settings, setSettings] = useState<ProjectSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [valueInput, setValueInput] = useState("");
  const [valueStatus, setValueStatus] = useState<string | null>(null);
  const [runbooks, setRunbooks] = useState<Runbook[]>([]);
  const [rbForm, setRbForm] = useState({ ...EMPTY_RUNBOOK });
  const [rbEditing, setRbEditing] = useState<number | null>(null);
  const [rbStatus, setRbStatus] = useState<string | null>(null);

  // Only the newest request's answer is shown: a slow answer for the previous key must not
  // replace the runbooks of the project the new key opens
  const runbooksRequest = useRef(0);
  const loadRunbooks = useCallback(() => {
    const request = ++runbooksRequest.current;
    if (!readKey) return;
    fetch(apiUrl(endpoint, "/api/runbooks"), { headers: { "X-API-Key": readKey } })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => request === runbooksRequest.current && setRunbooks(Array.isArray(data) ? data : []))
      .catch(() => request === runbooksRequest.current && setRunbooks([]));
  }, [readKey, endpoint]);

  useEffect(() => {
    loadRunbooks();
  }, [loadRunbooks]);

  // Add a runbook, or save the one being edited; empty matchers are sent as null
  const saveRunbook = async (e: React.FormEvent) => {
    e.preventDefault();
    const body = Object.fromEntries(Object.entries(rbForm).map(([k, v]) => [k, k === "title" || k === "steps" ? v : v.trim() || null]));
    try {
      const res = await fetch(apiUrl(endpoint, rbEditing ? `/api/runbooks/${rbEditing}` : "/api/runbooks"), {
        method: rbEditing ? "PUT" : "POST",
        headers: changeHeaders(readKey),
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(apiError(await res.json().catch(() => null), res.status));
      setRbStatus(rbEditing ? "Saved" : "Added");
      setRbForm({ ...EMPTY_RUNBOOK });
      setRbEditing(null);
      loadRunbooks();
    } catch (err: any) {
      setRbStatus(`Could not save: ${err.message}`);
    }
  };

  const editRunbook = (rb: Runbook) => {
    setRbEditing(rb.id);
    setRbStatus(null);
    setRbForm({
      title: rb.title,
      steps: rb.steps,
      match_exception: rb.match_exception || "",
      match_path: rb.match_path || "",
      match_route: rb.match_route || "",
      url: rb.url || "",
    });
  };

  const deleteRunbook = async (rb: Runbook) => {
    if (!window.confirm(`Delete the runbook "${rb.title}"?`)) return;
    const res = await fetch(apiUrl(endpoint, `/api/runbooks/${rb.id}`), { method: "DELETE", headers: changeHeaders(readKey) });
    setRbStatus(res.ok ? "Deleted" : `Could not delete: ${apiError(await res.json().catch(() => null), res.status)}`);
    loadRunbooks();
  };

  // Value of one failed request, used to estimate what an incident cost
  const saveFailedRequestValue = async () => {
    try {
      const micros = dollarsToMicros(valueInput) ?? 0;
      const res = await fetch(apiUrl(endpoint, "/api/settings"), {
        method: "PUT",
        headers: changeHeaders(readKey),
        body: JSON.stringify({ failed_request_value_micros: micros }),
      });
      if (!res.ok) throw new Error(apiError(await res.json().catch(() => null), res.status));
      setSettings((prev) => (prev ? { ...prev, failed_request_value_micros: micros || null } : prev));
      setValueStatus(micros ? "Saved" : "Cleared");
    } catch (e: any) {
      setValueStatus(`Could not save: ${e.message}`);
    }
  };

  useEffect(() => {
    const key = loadReadKey();
    const ep = loadEndpoint();
    setReadKey(key);
    setEndpoint(ep);
    setDraftKey(key);
    setDraftEndpoint(ep);
    setHasManage(Boolean(loadManageKey()));
  }, []);

  useEffect(() => {
    // A new key may open another project: forget what the old one showed, and the runbook being
    // edited, so nothing on screen belongs to a different project than the one a Save writes to
    setSettings(null);
    setRunbooks([]);
    setRbEditing(null);
    setRbForm({ ...EMPTY_RUNBOOK });
    setRbStatus(null);
    setValueInput("");
    setValueStatus(null);
    setError(null);
    setLoading(!!readKey);
    if (!readKey) return;
    let cancelled = false;
    fetch(apiUrl(endpoint, "/api/settings"), { headers: { "X-API-Key": readKey } })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.status === 401 || res.status === 403 ? "That read key was not accepted." : `The API answered ${res.status}.`);
        return res.json();
      })
      .then((data: ProjectSettings) => !cancelled && setSettings(data))
      .catch((err: Error) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [readKey, endpoint]);

  // The manage key is kept for this tab only, and never shown again once entered
  const applyManageKey = (e: React.FormEvent) => {
    e.preventDefault();
    saveManageKey(draftManage);
    setHasManage(Boolean(draftManage.trim()));
    setDraftManage("");
  };

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    saveReadKey(draftKey);
    saveEndpoint(draftEndpoint);
    setReadKey(draftKey.trim());
    setEndpoint(draftEndpoint.trim());
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30 px-6 py-3.5 flex flex-wrap items-center gap-4 justify-between">
        <h1 className="text-lg font-semibold">Settings</h1>
        <nav className="flex flex-wrap items-center gap-2">
          <NavLinks />
        </nav>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 py-8 space-y-8">
        <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
          <h2 className="font-semibold mb-1">Dashboard access</h2>
          <p className="text-sm text-slate-400 mb-4">
            Saved in this browser only and used by every page. Use your project&apos;s read key (<code>cw_read_…</code>). It sees
            this project only and cannot send events, export or delete data; it can acknowledge, assign and resolve
            incidents and start investigations. Changing settings, the budget, mute rules or runbooks needs the manage key below.
          </p>
          <form onSubmit={save} className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] items-end">
            <label className="text-xs text-slate-400">
              Read key
              <input
                type="password"
                value={draftKey}
                onChange={(e) => setDraftKey(e.target.value)}
                placeholder="cw_read_…"
                autoComplete="off"
                className="mt-1 w-full rounded-md bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-slate-100"
              />
            </label>
            <label className="text-xs text-slate-400">
              API address (leave empty to use this dashboard&apos;s address)
              <input
                value={draftEndpoint}
                onChange={(e) => setDraftEndpoint(e.target.value)}
                placeholder="same origin"
                className="mt-1 w-full rounded-md bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-slate-100"
              />
            </label>
            <button type="submit" className="rounded-md bg-brand hover:bg-teal-700 px-4 py-2 text-sm font-medium text-white">
              Save
            </button>
          </form>
          <form onSubmit={applyManageKey} className="mt-4 grid gap-3 sm:grid-cols-[1fr_auto] items-end">
            <label className="text-xs text-slate-400">
              Manage key, to change settings, the budget and runbooks (<code>cw_manage_…</code>, kept for this tab only;
              the admin key saved on the Owner page works too)
              <input
                type="password"
                value={draftManage}
                onChange={(e) => setDraftManage(e.target.value)}
                placeholder={hasManage ? "entered for this tab" : "cw_manage_…"}
                autoComplete="off"
                className="mt-1 w-full rounded-md bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-slate-100"
              />
            </label>
            <button type="submit" className="rounded-md bg-slate-800 border border-slate-700 hover:bg-slate-700 px-4 py-2 text-sm">
              {draftManage.trim() || !hasManage ? "Use" : "Forget"}
            </button>
          </form>
        </section>

        {!readKey && <p className="text-sm text-slate-400">Enter a read key above to see this project&apos;s settings.</p>}
        {loading && <p className="text-sm text-slate-400">Loading settings…</p>}
        {error && <p className="text-sm text-rose-400">{error}</p>}

        {settings && !loading && (
          <>
            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-3">Project</h2>
              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
                <dt className="text-slate-400">Name</dt>
                <dd>{settings.name}</dd>
                <dt className="text-slate-400">Quiet hours</dt>
                <dd>{settings.notify_quiet ? "On: low-severity items go to the weekly digest" : "Off"}</dd>
                <dt className="text-slate-400">Value of a failed request</dt>
                <dd className="flex flex-wrap items-center gap-2">
                  <span>{settings.failed_request_value_micros ? `$${microsToDollars(settings.failed_request_value_micros)}` : "Not set"}</span>
                  <input
                    value={valueInput}
                    onChange={(e) => setValueInput(e.target.value)}
                    placeholder="e.g. 0.50"
                    className="w-24 rounded bg-slate-950 border border-slate-700 px-2 py-1 text-xs"
                  />
                  <button onClick={saveFailedRequestValue} className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-xs hover:bg-slate-700">
                    Save
                  </button>
                  {valueStatus && <span className="text-xs text-slate-400">{valueStatus}</span>}
                  <span className="basis-full text-xs text-slate-500">Used to estimate what an incident cost (failed requests × this value). Empty and save to clear.</span>
                </dd>
              </dl>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Alert channels</h2>
              <p className="text-sm text-slate-400 mb-3">Where incidents, verdicts and AI explanations are posted. Secrets are stored encrypted and never shown here.</p>
              {settings.channels.length === 0 ? (
                <p className="text-sm text-slate-400">
                  No channels yet: alerts go to the project&apos;s Slack webhook if one was set at creation. Add one with{" "}
                  <code className="text-slate-300">python -m app.cli add-channel {settings.id} discord --config &apos;{"{"}&quot;webhook_url&quot;: &quot;…&quot;{"}"}&apos;</code>
                </p>
              ) : (
                <ul className="divide-y divide-slate-800">
                  {settings.channels.map((ch) => (
                    <li key={ch.id} className="flex items-center justify-between py-2 text-sm">
                      <span>{CHANNEL_LABELS[ch.type] || ch.type}</span>
                      <span className={ch.enabled ? "text-emerald-400" : "text-slate-500"}>{ch.enabled ? "Enabled" : "Disabled"}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Team members</h2>
              <p className="text-sm text-slate-400 mb-3">Maps git authors to chat handles so alerts mention the right owner.</p>
              {settings.team_members.length === 0 ? (
                <p className="text-sm text-slate-400">
                  No members yet: alerts show CODEOWNERS entries and git authors as plain text. Add one with{" "}
                  <code className="text-slate-300">python -m app.cli add-member</code>.
                </p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="text-left text-slate-400">
                    <tr>
                      <th className="py-1 font-normal">Name</th>
                      <th className="py-1 font-normal">Git emails</th>
                      <th className="py-1 font-normal">Chat</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800">
                    {settings.team_members.map((m) => (
                      <tr key={m.id}>
                        <td className="py-2">{m.name}</td>
                        <td className="py-2 text-slate-300">{(m.git_emails || []).join(", ") || "-"}</td>
                        <td className="py-2 text-slate-300">
                          {[m.slack_user_id && "Slack", m.discord_user_id && "Discord", m.email && "Email"].filter(Boolean).join(", ") || "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Runbooks</h2>
              <p className="text-sm text-slate-400 mb-3">
                Steps for a kind of error. The best match is linked in the alert and shown on the incident page, and the AI
                investigation reads it. Set at least one of error type, file or route; when several are set, all must match.
                Patterns use <code>*</code>, and a pattern ending in <code>/</code> means everything under it.
              </p>
              {runbooks.length === 0 ? (
                <p className="text-sm text-slate-400 mb-4">No runbooks yet.</p>
              ) : (
                <ul className="divide-y divide-slate-800 mb-4">
                  {runbooks.map((rb) => (
                    <li key={rb.id} className="py-2 text-sm flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="font-medium">{rb.title}</span>
                      <span className="text-xs text-slate-400 font-mono">
                        {[rb.match_exception, rb.match_path, rb.match_route].filter(Boolean).join(" · ")}
                      </span>
                      <span className="ml-auto flex gap-2">
                        <button onClick={() => editRunbook(rb)} className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-xs hover:bg-slate-700">
                          Edit
                        </button>
                        <button onClick={() => deleteRunbook(rb)} className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-xs text-rose-300 hover:bg-slate-700">
                          Delete
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <form onSubmit={saveRunbook} className="grid gap-3 sm:grid-cols-2">
                {([
                  ["title", "Title (shown in alerts)", "Billing totals fail"],
                  ["url", "Link to your own page (https, optional)", "https://wiki.example.com/runbooks/billing"],
                  ["match_exception", "Error type", "ZeroDivisionError"],
                  ["match_path", "File", "app/billing/"],
                  ["match_route", "Route", "/checkout*"],
                ] as const).map(([key, label, placeholder]) => (
                  <label key={key} className="text-xs text-slate-400">
                    {label}
                    <input
                      value={rbForm[key]}
                      onChange={(e) => setRbForm({ ...rbForm, [key]: e.target.value })}
                      placeholder={placeholder}
                      className="mt-1 w-full rounded-md bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-slate-100"
                    />
                  </label>
                ))}
                <label className="text-xs text-slate-400 sm:col-span-2">
                  Steps
                  <textarea
                    value={rbForm.steps}
                    onChange={(e) => setRbForm({ ...rbForm, steps: e.target.value })}
                    rows={5}
                    placeholder={"1. Check the discounts table for zero values\n2. Roll back if more than 1% of checkouts fail"}
                    className="mt-1 w-full rounded-md bg-slate-950 border border-slate-700 px-3 py-2 text-sm text-slate-100"
                  />
                </label>
                <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
                  <button type="submit" className="rounded-md bg-brand hover:bg-teal-700 px-4 py-2 text-sm font-medium text-white">
                    {rbEditing ? "Save runbook" : "Add runbook"}
                  </button>
                  {rbEditing && (
                    <button
                      type="button"
                      onClick={() => {
                        setRbEditing(null);
                        setRbForm({ ...EMPTY_RUNBOOK });
                      }}
                      className="rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm hover:bg-slate-700"
                    >
                      Cancel
                    </button>
                  )}
                  {rbStatus && <span className="text-xs text-slate-400">{rbStatus}</span>}
                </div>
              </form>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
              <h2 className="font-semibold mb-1">Critical routes</h2>
              <p className="text-sm text-slate-400 mb-3">A single error on these paths opens an incident immediately (otherwise 3 errors within 60 s).</p>
              {settings.critical_routes.length === 0 ? (
                <p className="text-sm text-slate-400">None configured.</p>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {settings.critical_routes.map((route) => (
                    <li key={route} className="rounded bg-slate-800 px-2 py-1 text-xs font-mono">
                      {route}
                    </li>
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
