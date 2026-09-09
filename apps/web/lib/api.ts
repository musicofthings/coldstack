const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Lead = {
  full_name: string | null; first_name: string | null; last_name: string | null;
  title: string | null; seniority: string | null; department: string | null;
  company: string | null; domain: string | null; industry: string | null;
  headcount: number | null; country: string | null; linkedin_url: string | null;
  email: string | null; email_source: string | null; email_status: string;
  cost_usd: number;
};

export type Report = {
  searched: number; after_dedupe: number; after_suppression: number;
  emails_found: number; verified_valid: number; total_cost_usd: number;
  per_provider: Record<string, { attempts: number; hits: number; cost: number }>;
  unsupported_filters: string[]; budget_exhausted: boolean;
  cost_per_valid_email?: number;
};

export type Job = {
  id: string; kind: string; status: "queued" | "running" | "done" | "failed" | "cancelled";
  created_at: string; finished_at: string | null;
  error?: string; report?: Report; count?: number;
};

export type Query = {
  titles: string[]; seniorities: string[]; countries: string[]; industries: string[];
  keywords: string[]; headcount_min: number | null; headcount_max: number | null;
  limit: number;
};

export type Preview = {
  sample: Array<Record<string, string | number | null>>;
  total_available: number | null;
  unsupported_filters: string[];
  estimated_enrichment_usd: number;
  note: string;
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => call<{ ok: boolean; credential_key: string }>("/health"),
  credentials: () => call<{ credentials: Array<{ provider: string; hints: Record<string, string>; status: string }> }>("/credentials"),
  putCredential: (provider: string, api_key: string) =>
    call(`/credentials/${provider}`, { method: "PUT", body: JSON.stringify({ creds: { api_key } }) }),
  testCredential: (provider: string) =>
    call<{ ok: boolean; error?: string }>(`/credentials/${provider}/test`, { method: "POST" }),
  preview: (query: Query, demo: boolean) =>
    call<Preview>("/search/preview", { method: "POST", body: JSON.stringify({ query, demo }) }),
  build: (query: Query, demo: boolean, budget_usd: number | null) =>
    call<Job>("/lists/build", { method: "POST", body: JSON.stringify({ query, demo, budget_usd }) }),
  job: (id: string) => call<Job>(`/jobs/${id}`),
  cancel: (id: string) => call(`/jobs/${id}/cancel`, { method: "POST" }),
  leads: (id: string, onlyValid: boolean) =>
    call<{ total: number; leads: Lead[] }>(`/jobs/${id}/leads?only_valid=${onlyValid}&limit=200`),
  csvUrl: (id: string, onlyValid: boolean) =>
    `${BASE}/jobs/${id}/export.csv?only_valid=${onlyValid}`,
};
