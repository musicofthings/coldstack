"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Job, type Lead, type Preview, type Query } from "@/lib/api";

const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function Page() {
  const [demo, setDemo] = useState(true);
  const [keys, setKeys] = useState({ apollo: "", hunter: "" });
  const [keyStatus, setKeyStatus] = useState<Record<string, string>>({});
  const [titles, setTitles] = useState("Head of Laboratory, Director of Genomics");
  const [countries, setCountries] = useState("India");
  const [seniorities, setSeniorities] = useState("");
  const [headMin, setHeadMin] = useState("20");
  const [headMax, setHeadMax] = useState("500");
  const [limit, setLimit] = useState("200");
  const [budget, setBudget] = useState("");
  const [onlyValid, setOnlyValid] = useState(true);

  const [preview, setPreview] = useState<Preview | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [busy, setBusy] = useState<"" | "preview" | "build">("");
  const [error, setError] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  const query = (): Query => ({
    titles: csv(titles), seniorities: csv(seniorities), countries: csv(countries),
    industries: [], keywords: [],
    headcount_min: headMin ? Number(headMin) : null,
    headcount_max: headMax ? Number(headMax) : null,
    limit: Number(limit) || 100,
  });

  const saveKey = async (provider: "apollo" | "hunter") => {
    setError(null);
    try {
      await api.putCredential(provider, keys[provider]);
      const r = await api.testCredential(provider);
      setKeyStatus((s) => ({ ...s, [provider]: r.ok ? "connected" : r.error ?? "rejected" }));
      // Never keep the plaintext key in browser state once the server has sealed it.
      setKeys((k) => ({ ...k, [provider]: "" }));
    } catch (e) { setError((e as Error).message); }
  };

  const runPreview = async () => {
    setBusy("preview"); setError(null);
    try { setPreview(await api.preview(query(), demo)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  };

  const stopPolling = useCallback(() => {
    if (poll.current) { clearInterval(poll.current); poll.current = null; }
  }, []);

  const runBuild = async () => {
    setBusy("build"); setError(null); setLeads([]); stopPolling();
    try {
      const started = await api.build(query(), demo, budget ? Number(budget) : null);
      setJob(started);
      poll.current = setInterval(async () => {
        try {
          const j = await api.job(started.id);
          setJob(j);
          if (j.status !== "queued" && j.status !== "running") {
            stopPolling(); setBusy("");
            if (j.status === "done") setLeads((await api.leads(j.id, onlyValid)).leads);
          }
        } catch (e) { stopPolling(); setBusy(""); setError((e as Error).message); }
      }, 700);
    } catch (e) { setBusy(""); setError((e as Error).message); }
  };

  // Refetch the table when the valid-only filter changes on a finished job.
  useEffect(() => {
    if (job?.status === "done") api.leads(job.id, onlyValid).then((r) => setLeads(r.leads));
  }, [onlyValid, job?.status, job?.id]);

  useEffect(() => stopPolling, [stopPolling]);

  const report = job?.report;
  const running = busy === "build" || job?.status === "running" || job?.status === "queued";

  return (
    <main>
      <h1>ColdStack</h1>
      <p className="sub">Open-source, BYOK list building. Search finds people; emails are a separate, priced step.</p>

      <section className="panel">
        <h2>Data sources</h2>
        <div className="row" style={{ marginBottom: 14 }}>
          <label style={{ margin: 0 }}>
            <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />{" "}
            Demo mode — fake providers, no keys, no spend
          </label>
        </div>
        {!demo && (
          <div className="grid">
            {(["apollo", "hunter"] as const).map((p) => (
              <div key={p}>
                <label htmlFor={p}>{p === "apollo" ? "Apollo API key (search)" : "Hunter API key (email finding + verification)"}</label>
                <div className="row">
                  <input id={p} type="password" placeholder="paste key" style={{ flex: 1 }}
                    value={keys[p]} onChange={(e) => setKeys((k) => ({ ...k, [p]: e.target.value }))} />
                  <button onClick={() => saveKey(p)} disabled={!keys[p]}>Save &amp; test</button>
                </div>
                {keyStatus[p] && (
                  <div className="muted mono" style={{ marginTop: 6 }}>
                    {keyStatus[p] === "connected" ? "✓ connected" : `✕ ${keyStatus[p]}`}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Audience</h2>
        <div className="grid">
          <div><label htmlFor="t">Job titles (comma separated)</label>
            <input id="t" type="text" value={titles} onChange={(e) => setTitles(e.target.value)} /></div>
          <div><label htmlFor="c">Countries</label>
            <input id="c" type="text" value={countries} onChange={(e) => setCountries(e.target.value)} /></div>
          <div><label htmlFor="s">Seniorities</label>
            <input id="s" type="text" placeholder="c_suite, vp, director" value={seniorities} onChange={(e) => setSeniorities(e.target.value)} /></div>
          <div><label htmlFor="hmin">Headcount min</label>
            <input id="hmin" type="number" value={headMin} onChange={(e) => setHeadMin(e.target.value)} /></div>
          <div><label htmlFor="hmax">Headcount max</label>
            <input id="hmax" type="number" value={headMax} onChange={(e) => setHeadMax(e.target.value)} /></div>
          <div><label htmlFor="l">Limit</label>
            <input id="l" type="number" value={limit} onChange={(e) => setLimit(e.target.value)} /></div>
          <div><label htmlFor="b">Budget cap (USD, optional)</label>
            <input id="b" type="number" step="0.01" placeholder="no cap" value={budget} onChange={(e) => setBudget(e.target.value)} /></div>
        </div>
        <div className="row" style={{ marginTop: 16 }}>
          <button onClick={runPreview} disabled={busy !== ""}>
            {busy === "preview" ? "Searching…" : "Preview (free)"}
          </button>
          <button className="primary" onClick={runBuild} disabled={busy !== ""}>
            {running ? "Building…" : "Build list"}
          </button>
          {running && job && <button onClick={() => api.cancel(job.id).catch(() => {})}>Cancel</button>}
          <div className="spacer" />
          <label style={{ margin: 0 }}>
            <input type="checkbox" checked={onlyValid} onChange={(e) => setOnlyValid(e.target.checked)} />{" "}
            Verified only
          </label>
        </div>
        {error && <div className="err">{error}</div>}
      </section>

      {preview && (
        <section className="panel">
          <h2>Preview — {preview.total_available ?? preview.sample.length} matches</h2>
          <div className="note">
            {preview.note}. Enriching {limit} of these is estimated at about ${preview.estimated_enrichment_usd}.
          </div>
          {preview.unsupported_filters.map((w) => <div className="note" key={w}>{w}</div>)}
          <div className="scroll" style={{ marginTop: 14 }}>
            <table>
              <thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Headcount</th></tr></thead>
              <tbody>
                {preview.sample.slice(0, 10).map((p, i) => (
                  <tr key={i}>
                    <td>{p.full_name}</td><td>{p.title}</td>
                    <td>{p.company} <span className="muted mono">{p.domain}</span></td>
                    <td>{p.headcount}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {report && (
        <section className="panel">
          <h2>Result</h2>
          <div className="stats">
            <div className="stat"><div className="v">{report.searched}</div><div className="k">searched</div></div>
            <div className="stat"><div className="v">{report.after_suppression}</div><div className="k">after suppression</div></div>
            <div className="stat"><div className="v">{report.emails_found}</div><div className="k">emails found</div></div>
            <div className="stat"><div className="v">{report.verified_valid}</div><div className="k">verified valid</div></div>
            <div className="stat"><div className="v">${report.total_cost_usd.toFixed(4)}</div><div className="k">total cost</div></div>
            <div className="stat"><div className="v">${(report.cost_per_valid_email ?? 0).toFixed(4)}</div><div className="k">per valid email</div></div>
          </div>
          {report.budget_exhausted && <div className="note">Stopped early — budget cap reached. Raise the cap or narrow the audience.</div>}
          {report.unsupported_filters.map((w) => <div className="note" key={w}>{w}</div>)}

          <div className="scroll" style={{ marginTop: 16 }}>
            <table>
              <thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Status</th><th>Source</th></tr></thead>
              <tbody>
                {leads.slice(0, 50).map((l, i) => (
                  <tr key={i}>
                    <td>{l.full_name}</td><td>{l.title}</td><td>{l.company}</td>
                    <td className="mono">{l.email}</td>
                    <td><span className={`tag ${l.email_status}`}>{l.email_status}</span></td>
                    <td className="muted">{l.email_source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="row" style={{ marginTop: 16 }}>
            <a href={api.csvUrl(job!.id, onlyValid)}><button className="primary">Download CSV</button></a>
            <span className="muted">
              showing {Math.min(leads.length, 50)} of {leads.length}
              {onlyValid ? " verified" : ""} rows
            </span>
          </div>
        </section>
      )}
    </main>
  );
}
