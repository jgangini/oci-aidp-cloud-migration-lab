import { FormEvent, useEffect, useState } from "react";

type ApiError = { detail?: string };
type LabUser = { id: string; name: string; email: string; status: "active" | "pending"; active: boolean };

class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    throw new ApiRequestError(body.detail || `Request failed (${response.status})`, response.status);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

function Brand() {
  return (
    <a className="brand" href="/" aria-label="OCI AI Data Platform Lab home">
      <span className="brand-mark" aria-hidden="true">AI</span>
      <span><strong>OCI AIDP</strong><small>Cloud Migration Lab</small></span>
    </a>
  );
}

function Shell({ children, adminLink = true }: { children: React.ReactNode; adminLink?: boolean }) {
  return (
    <div className="page-shell">
      <header><Brand />{adminLink && <a className="quiet-link" href="/admin/login">Administrator login</a>}</header>
      <main>{children}</main>
      <footer>Oracle and Java are registered trademarks of Oracle and/or its affiliates.</footer>
    </div>
  );
}

function RegisterPage() {
  const [form, setForm] = useState({ name: "", email: "", password: "", code: "" });
  const [state, setState] = useState<{ busy: boolean; message: string; error: boolean }>({ busy: false, message: "", error: false });
  const update = (name: keyof typeof form, value: string) => setForm(current => ({ ...current, [name]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setState({ busy: true, message: "", error: false });
    try {
      const result = await api<{ status: string; message?: string }>("/api/register", { method: "POST", body: JSON.stringify(form) });
      setForm(current => ({ ...current, password: "", code: "" }));
      setState({ busy: false, error: false, message: result.status === "pending" ? "Your account is being reconciled. You can retry shortly." : "Your lab account is ready. Open AIDP from the OCI Console." });
    } catch (error) {
      setForm(current => ({ ...current, password: "", code: "" }));
      setState({ busy: false, error: true, message: error instanceof Error ? error.message : "Registration failed" });
    }
  }

  return (
    <Shell>
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Structured data · notebooks · medallion architecture</p>
          <h1>Build in a governed AI data workspace.</h1>
          <p className="lede">Register for this temporary lab to work with landing, bronze, silver and gold data layers in Oracle AI Data Platform.</p>
          <ol className="steps"><li>Create your lab identity</li><li>Open AIDP in the OCI Console</li><li>Start a notebook in the shared workspace</li></ol>
        </div>
        <form className="card" onSubmit={submit} aria-busy={state.busy}>
          <div><p className="eyebrow">Lab access</p><h2>Create your account</h2><p>Use your work or personal email and the code supplied by the instructor.</p></div>
          <label>Full name<input autoComplete="name" value={form.name} onChange={e => update("name", e.target.value)} minLength={2} maxLength={120} required /></label>
          <label>Email<input type="email" autoComplete="email" value={form.email} onChange={e => update("email", e.target.value)} required /></label>
          <label>Password<input type="password" autoComplete="new-password" value={form.password} onChange={e => update("password", e.target.value)} minLength={8} maxLength={256} required /></label>
          <label>Registration code<input className="code" autoComplete="off" value={form.code} onChange={e => update("code", e.target.value.toUpperCase())} pattern="[A-Z]{4}-[0-9]{4}" placeholder="ABCD-1234" maxLength={9} required /></label>
          {state.message && <p className={state.error ? "notice error" : "notice success"} role="status">{state.message}</p>}
          <button disabled={state.busy}>{state.busy ? "Creating account…" : "Create lab account"}</button>
          <small className="fine-print">Your password is sent directly to OCI Identity Domains over HTTPS and is never stored by this site.</small>
        </form>
      </section>
    </Shell>
  );
}

function AdminLogin() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    try {
      await api("/api/admin/login", { method: "POST", body: JSON.stringify({ username, password }) });
      setPassword(""); window.location.assign("/admin/users");
    } catch (reason) { setPassword(""); setError(reason instanceof Error ? reason.message : "Login failed"); }
  }
  return <Shell adminLink={false}><section className="centered"><form className="card narrow" onSubmit={submit}><p className="eyebrow">Restricted</p><h1>Administrator login</h1><label>Username<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required /></label><label>Password<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>{error && <p className="notice error" role="alert">{error}</p>}<button>Sign in</button><a className="quiet-link" href="/">Return to registration</a></form></section></Shell>;
}

function AdminUsers() {
  const [users, setUsers] = useState<LabUser[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { api<{ users: LabUser[] }>("/api/admin/users").then(result => setUsers(result.users)).catch(reason => { if (reason instanceof ApiRequestError && reason.status === 401) window.location.assign("/admin/login"); else setError(reason instanceof Error ? reason.message : "Unable to load users"); }); }, []);
  const visible = users.filter(user => `${user.name} ${user.email}`.toLowerCase().includes(query.toLowerCase()));
  async function logout() { await api("/api/admin/logout", { method: "POST" }); window.location.assign("/"); }
  return <Shell adminLink={false}><section className="admin"><div className="admin-heading"><div><p className="eyebrow">Identity Domains</p><h1>Lab users</h1><p>Live membership in the managed developer and pending groups.</p></div><button className="secondary" onClick={logout}>Sign out</button></div><label className="search">Search users<input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Name or email" /></label>{error && <p className="notice error">{error}</p>}<div className="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Status</th><th>Identity active</th></tr></thead><tbody>{visible.map(user => <tr key={user.id}><td>{user.name}</td><td>{user.email}</td><td><span className={`badge ${user.status}`}>{user.status}</span></td><td>{user.active ? "Yes" : "No"}</td></tr>)}{!visible.length && <tr><td colSpan={4} className="empty">No matching lab users.</td></tr>}</tbody></table></div></section></Shell>;
}

export function App() {
  if (window.location.pathname === "/admin/login") return <AdminLogin />;
  if (window.location.pathname === "/admin/users") return <AdminUsers />;
  return <RegisterPage />;
}
