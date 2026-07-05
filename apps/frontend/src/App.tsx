import {
  FormEvent,
  KeyboardEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

type ApiError = { detail?: string };
type LabUser = {
  id: string;
  name: string;
  email: string;
  status: "active" | "pending";
  active: boolean;
  managed?: boolean;
};

const focusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function ConfirmModal({
  open,
  kind,
  title,
  description,
  error,
  confirmLabel,
  onClose,
  onConfirm,
}: {
  open: boolean;
  kind: "question" | "delete";
  title: string;
  description: string;
  error?: string;
  confirmLabel: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    closeRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ??
          [],
      );
      if (!focusable.length) {
        event.preventDefault();
        panelRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocusRef.current?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  const icon =
    kind === "delete" ? (
      <TrashIcon />
    ) : (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M8.23 9c.55-1.17 2.03-2 3.77-2 2.21 0 4 1.34 4 3 0 1.4-1.28 2.58-3.01 2.91-.54.1-.99.54-.99 1.09m0 3h.01M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z"
        />
      </svg>
    );
  return createPortal(
    <div className="confirm-overlay">
      <section
        className={`confirm-modal confirm-${kind}`}
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <div className="confirm-content">
          <div className="confirm-icon">{icon}</div>
          <h2 id={titleId}>{title}</h2>
          <p id={descriptionId}>{description}</p>
          {error && (
            <p className="confirm-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer>
          <button ref={closeRef} type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="confirm-primary" type="button" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}

function secureRandomInt(limit: number) {
  const ceiling = Math.floor(0x100000000 / limit) * limit;
  const value = new Uint32Array(1);
  do {
    crypto.getRandomValues(value);
  } while (value[0] >= ceiling);
  return value[0] % limit;
}

function generatePassword() {
  const groups = [
    "ABCDEFGHJKLMNPQRSTUVWXYZ",
    "abcdefghijkmnopqrstuvwxyz",
    "23456789",
    "!@#$%*-_",
  ];
  const characters = groups.map(
    (group) => group[secureRandomInt(group.length)],
  );
  const alphabet = groups.join("");
  while (characters.length < 18)
    characters.push(alphabet[secureRandomInt(alphabet.length)]);
  for (let index = characters.length - 1; index > 0; index -= 1) {
    const swap = secureRandomInt(index + 1);
    [characters[index], characters[swap]] = [
      characters[swap],
      characters[index],
    ];
  }
  return characters.join("");
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    throw new ApiRequestError(
      body.detail || `Request failed (${response.status})`,
      response.status,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : ((await response.json()) as T);
}

function OracleMark() {
  return (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M.1 8c0 2.761 2.237 5 4.997 5h5.806A4.999 4.999 0 0015.9 8c0-2.761-2.237-5-4.997-5H5.097A4.999 4.999 0 00.1 8zm13.911 0a3.235 3.235 0 01-3.234 3.237h-5.55A3.235 3.235 0 011.991 8a3.235 3.235 0 013.234-3.236h5.551A3.235 3.235 0 0114.011 8z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function AdminLoginIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeMiterlimit="10"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
      />
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M2 12.88v-1.76c0-1.04.85-1.9 1.9-1.9 1.81 0 2.55-1.28 1.64-2.85a1.9 1.9 0 0 1 .7-2.59l1.73-.99a1.9 1.9 0 0 1 2.28.6l.11.19c.9 1.57 2.38 1.57 3.29 0l.11-.19a1.9 1.9 0 0 1 2.28-.6l1.73.99a1.9 1.9 0 0 1 .7 2.59c-.91 1.57-.17 2.85 1.64 2.85 1.04 0 1.9.85 1.9 1.9v1.76c0 1.04-.85 1.9-1.9 1.9-1.81 0-2.55 1.28-1.64 2.85a1.9 1.9 0 0 1-.7 2.59l-1.73.99a1.9 1.9 0 0 1-2.28-.6l-.11-.19c-.9-1.57-2.38-1.57-3.29 0l-.11.19a1.9 1.9 0 0 1-2.28.6l-1.73-.99a1.9 1.9 0 0 1-.7-2.59c.91-1.57.17-2.85-1.64-2.85-1.05 0-1.9-.85-1.9-1.9Z"
      />
    </svg>
  );
}

function WandIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path d="M3.844 7.922a2.886 2.886 0 0 1 4.078-4.078l12.233 12.233a2.886 2.886 0 0 1-4.078 4.078L3.844 7.922Z" />
      <path strokeLinecap="round" d="m6 10 4-4" />
      <path d="M16.1 2.307c.161-.409.739-.409.9 0l.43 1.095c.049.125.148.224.273.273l1.09.432c.409.161.409.741 0 .903l-1.09.432a.458.458 0 0 0-.273.273L17 6.811c-.161.409-.739.409-.9 0l-.43-1.095a.458.458 0 0 0-.273-.273l-1.091-.432a.487.487 0 0 1 0-.903l1.091-.432a.458.458 0 0 0 .273-.273l.43-1.096ZM19.967 9.129c.161-.409.739-.409.899 0l.157.4c.05.125.148.224.273.273l.398.158c.408.161.408.741 0 .903l-.398.157a.458.458 0 0 0-.273.273l-.157.4c-.16.409-.738.409-.9 0l-.156-.4a.458.458 0 0 0-.273-.273l-.398-.157a.487.487 0 0 1 0-.903l.398-.158a.458.458 0 0 0 .273-.273l.156-.4ZM5.133 15.307c.161-.409.739-.409.9 0l.157.4c.05.125.148.224.273.273l.398.157c.408.162.408.742 0 .903l-.398.158a.458.458 0 0 0-.273.273l-.157.399c-.161.41-.739.41-.9 0l-.157-.399a.458.458 0 0 0-.273-.273l-.398-.158a.487.487 0 0 1 0-.903l.398-.157a.458.458 0 0 0 .273-.273l.157-.4Z" />
    </svg>
  );
}

function EyeIcon({ hidden }: { hidden: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M2.5 12s3.4-5 9.5-5 9.5 5 9.5 5-3.4 5-9.5 5-9.5-5-9.5-5Z"
      />
      <circle cx="12" cy="12" r="2.5" />
      {hidden && <path strokeLinecap="round" d="M4 4l16 16" />}
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path strokeLinecap="round" d="m16 16 4.5 4.5" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <path strokeLinecap="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M20 11a8 8 0 0 0-14.8-4L3 10m0-6v6h6m-5 3a8 8 0 0 0 14.8 4L21 14m0 6v-6h-6"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 7h14m-9 4v6m4-6v6M9 7l.7-3h4.6l.7 3m-8.2 0 .7 13h9.2l.7-13"
      />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        fill="currentColor"
        d="M10.24 0a10 10 0 1 0 8.07 16.15.67.67 0 1 0-1.05-.82 8.9 8.9 0 1 1-.08-10.77.67.67 0 1 0 1.05-.82A9.95 9.95 0 0 0 10.24 0Zm6.86 7.16a.67.67 0 0 0-.94.95l1.56 1.53-10.26.01a.65.65 0 1 0 0 1.3l10.31-.01-1.55 1.56a.67.67 0 1 0 .95.94l2.64-2.64a.67.67 0 0 0-.01-.94L17.1 7.16Z"
      />
    </svg>
  );
}

function Brand() {
  return (
    <a className="brand" href="/" aria-label="AI Data Platform Workbench home">
      <span className="brand-mark">
        <OracleMark />
      </span>
      <span>
        <strong>AI Data Platform Workbench</strong>
        <small>Cloud Migration Lab</small>
      </span>
    </a>
  );
}

function Shell({
  children,
  adminLink = true,
  onSignOut,
}: {
  children: React.ReactNode;
  adminLink?: boolean;
  onSignOut?: () => void;
}) {
  const currentPath = window.location.pathname;
  return (
    <div className="page-shell">
      <div className="header-band">
        <header>
          <Brand />
          {onSignOut && (
            <nav className="admin-nav" aria-label="Admin navigation">
              <a
                href="/admin/users"
                aria-current={
                  currentPath === "/admin/users" ? "page" : undefined
                }
              >
                Users
              </a>
              <a
                href="/admin/settings"
                aria-current={
                  currentPath === "/admin/settings" ? "page" : undefined
                }
              >
                Settings
              </a>
            </nav>
          )}
          <div className="header-actions">
            {onSignOut ? (
              <button
                className="header-signout"
                type="button"
                onClick={onSignOut}
                aria-label="Logout"
                data-tooltip="Logout"
              >
                <LogoutIcon />
              </button>
            ) : (
              adminLink && (
                <a
                  className="admin-link"
                  href="/admin/login"
                  aria-label="Administrator login"
                  title="Administrator login"
                >
                  <AdminLoginIcon />
                </a>
              )
            )}
          </div>
        </header>
      </div>
      <main>{children}</main>
      <footer className="app-footer">
        <span>
          Made with{" "}
          <span className="footer-heart" aria-hidden="true">
            &#9829;
          </span>{" "}
          at AI CloudTech
        </span>
        <span className="footer-divider" aria-hidden="true">
          &middot;
        </span>
        <span>Developed by </span>
        <a
          href="https://www.linkedin.com/in/joelgangini"
          target="_blank"
          rel="noopener noreferrer"
        >
          Joel Gangini
        </a>
      </footer>
    </div>
  );
}

function RegisterPage() {
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [codeSlots, setCodeSlots] = useState<string[]>(() => Array(8).fill(""));
  const codeInputs = useRef<Array<HTMLInputElement | null>>([]);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [state, setState] = useState<{
    phase: "idle" | "processing" | "ready" | "error";
    message: string;
    aidpUrl?: string;
  }>({ phase: "idle", message: "" });
  const update = (name: keyof typeof form, value: string) =>
    setForm((current) => ({ ...current, [name]: value }));
  const registrationCode = `${codeSlots.slice(0, 4).join("")}-${codeSlots.slice(4).join("")}`;

  function focusCode(index: number) {
    codeInputs.current[Math.min(index, 7)]?.focus();
  }
  function setCodeSlot(index: number, value: string) {
    const character =
      value.toUpperCase().match(index < 4 ? /[A-Z]/ : /[0-9]/)?.[0] || "";
    setCodeSlots((current) =>
      current.map((slot, slotIndex) =>
        slotIndex === index ? character : slot,
      ),
    );
    if (character && index < 7)
      requestAnimationFrame(() => focusCode(index + 1));
  }
  function pasteCode(value: string) {
    const compact = value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!/^[A-Z]{1,4}[0-9]{0,4}$/.test(compact)) return;
    const next = Array(8).fill("");
    Array.from(compact).forEach((character, index) => {
      next[index] = character;
    });
    setCodeSlots(next);
    requestAnimationFrame(() => focusCode(Math.min(compact.length, 7)));
  }
  function handleCodeKeyDown(
    index: number,
    event: KeyboardEvent<HTMLInputElement>,
  ) {
    if (event.key !== "Backspace" || codeSlots[index]) return;
    if (index > 0) {
      event.preventDefault();
      setCodeSlots((current) =>
        current.map((slot, slotIndex) => (slotIndex === index - 1 ? "" : slot)),
      );
      requestAnimationFrame(() => focusCode(index - 1));
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!/^[A-Z]{4}-[0-9]{4}$/.test(registrationCode)) {
      setState({
        phase: "error",
        message: "Enter four letters followed by four numbers.",
      });
      focusCode(0);
      return;
    }
    const payload = { ...form, code: registrationCode };
    setState({
      phase: "processing",
      message: "Creating your Identity Domains account…",
    });
    try {
      let result: { status: string; message?: string; aidp_url?: string };
      for (let attempt = 0; ; attempt += 1) {
        result = await api<{
          status: string;
          message?: string;
          aidp_url?: string;
        }>("/api/register", { method: "POST", body: JSON.stringify(payload) });
        if (result.status !== "pending") break;
        if (attempt === 11)
          throw new Error(
            "OCI is still reconciling your access. Please try again shortly.",
          );
        setState({
          phase: "processing",
          message: "OCI is configuring your developer access…",
        });
        await new Promise((resolve) => window.setTimeout(resolve, 2_500));
      }
      if (!result.aidp_url)
        throw new Error("AIDP is ready, but its console link is unavailable.");
      setForm((current) => ({ ...current, password: "" }));
      setCodeSlots(Array(8).fill(""));
      setState({
        phase: "ready",
        message: "Your lab account is ready.",
        aidpUrl: result.aidp_url,
      });
    } catch (error) {
      setForm((current) => ({ ...current, password: "" }));
      setCodeSlots(Array(8).fill(""));
      setState({
        phase: "error",
        message: error instanceof Error ? error.message : "Registration failed",
      });
    }
  }

  return (
    <Shell>
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">
            Structured data · notebooks · medallion architecture
          </p>
          <h1>Build in a governed AI data workspace.</h1>
          <p className="lede">
            Register for this temporary lab to work with landing, bronze, silver
            and gold data layers in Oracle AI Data Platform.
          </p>
          <ol className="steps">
            <li className="step-card">
              <span className="step-number">01 · Identity</span>
              <strong>
                <span>Set up</span>
                <span>your account</span>
              </strong>
              <small>Register with your name, email and lab code.</small>
            </li>
            <li className="step-card">
              <span className="step-number">02 · Workbench</span>
              <strong>
                <span>Open AI Data</span>
                <span>Platform</span>
              </strong>
              <small>Enter the workspace from the Oracle Cloud Console.</small>
            </li>
            <li className="step-card">
              <span className="step-number">03 · Notebooks</span>
              <strong>Start a shared notebook</strong>
              <small>Work across the governed medallion data layers.</small>
            </li>
          </ol>
        </div>
        <form
          className="card"
          onSubmit={submit}
          aria-busy={state.phase === "processing"}
        >
          <div>
            <p className="eyebrow">Lab access</p>
            <h2>Create your account</h2>
            <p>
              Use your work or personal email and the code supplied by the
              instructor.
            </p>
          </div>
          <label>
            Full name
            <input
              autoComplete="name"
              value={form.name}
              onChange={(e) => update("name", e.target.value)}
              minLength={2}
              maxLength={120}
              required
            />
          </label>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              value={form.email}
              onChange={(e) => update("email", e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <span className="password-control">
              <input
                type={passwordVisible ? "text" : "password"}
                autoComplete="new-password"
                value={form.password}
                onChange={(e) => update("password", e.target.value)}
                minLength={8}
                maxLength={256}
                required
              />
              <span className="password-actions">
                <button
                  type="button"
                  className="password-action"
                  onClick={() => update("password", generatePassword())}
                  aria-label="Generate password"
                  title="Generate password"
                >
                  <WandIcon />
                </button>
                <button
                  type="button"
                  className="password-action"
                  onClick={() => setPasswordVisible((current) => !current)}
                  aria-label={
                    passwordVisible ? "Hide password" : "Show password"
                  }
                  title={passwordVisible ? "Hide password" : "Show password"}
                >
                  <EyeIcon hidden={passwordVisible} />
                </button>
              </span>
            </span>
          </label>
          <fieldset className="registration-code">
            <legend>Registration code</legend>
            <span id="registration-code-help" className="sr-only">
              Enter four letters followed by four numbers.
            </span>
            <div
              className="code-slots"
              onPaste={(event) => {
                event.preventDefault();
                pasteCode(event.clipboardData.getData("text"));
              }}
            >
              {codeSlots.map((value, index) => (
                <span className="code-slot-wrap" key={index}>
                  {index === 4 && (
                    <span className="code-separator" aria-hidden="true">
                      -
                    </span>
                  )}
                  <input
                    ref={(element) => {
                      codeInputs.current[index] = element;
                    }}
                    className="code-slot"
                    aria-label={`Registration code character ${index + 1} of 8`}
                    aria-describedby="registration-code-help"
                    autoComplete="off"
                    autoCapitalize="characters"
                    inputMode={index < 4 ? "text" : "numeric"}
                    maxLength={1}
                    value={value}
                    onChange={(event) => setCodeSlot(index, event.target.value)}
                    onKeyDown={(event) => handleCodeKeyDown(index, event)}
                    onFocus={(event) => event.currentTarget.select()}
                    required
                  />
                </span>
              ))}
            </div>
          </fieldset>
          {state.phase === "error" && (
            <p className="notice error" role="alert">
              {state.message}
            </p>
          )}
          <button disabled={state.phase === "processing"}>
            {state.phase === "processing"
              ? "Creating account…"
              : "Create account"}
          </button>
        </form>
      </section>
      {state.phase === "processing" && (
        <section
          className="registration-overlay"
          role="status"
          aria-live="polite"
        >
          <div className="registration-result">
            <span className="progress-orbit" aria-hidden="true" />
            <p className="eyebrow">Provisioning access</p>
            <h2>Preparing your lab</h2>
            <p>{state.message}</p>
          </div>
        </section>
      )}
      {state.phase === "ready" && (
        <section
          className="registration-overlay"
          role="status"
          aria-live="polite"
        >
          <div className="registration-result registration-result-ready">
            <p className="eyebrow">Access ready</p>
            <h2>Your lab account is ready</h2>
            <p>
              Open AI Data Platform to start working in the shared workspace.
            </p>
            <a
              className="result-link"
              href={state.aidpUrl}
              target="_blank"
              rel="noopener noreferrer"
            >
              Open AI Data Platform
            </a>
            <button
              className="secondary"
              type="button"
              onClick={() => setState({ phase: "idle", message: "" })}
            >
              Return to registration
            </button>
          </div>
        </section>
      )}
    </Shell>
  );
}

function AdminLogin() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      window.location.assign("/admin/users");
    } catch (reason) {
      setPassword("");
      setError(reason instanceof Error ? reason.message : "Login failed");
    }
  }
  return (
    <Shell adminLink={false}>
      <section className="centered">
        <form className="card narrow" onSubmit={submit}>
          <h1>Login</h1>
          <label>
            Username
            <input
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <button>Sign in</button>
          <a className="quiet-link" href="/">
            Return to registration
          </a>
        </form>
      </section>
    </Shell>
  );
}

function AdminUsers() {
  const [users, setUsers] = useState<LabUser[]>([]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({ name: "", email: "", password: "" });
  const [pendingDelete, setPendingDelete] = useState<LabUser | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [logoutOpen, setLogoutOpen] = useState(false);
  async function loadUsers() {
    try {
      setUsers((await api<{ users: LabUser[] }>("/api/admin/users")).users);
    } catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401)
        window.location.assign("/admin/login");
      else
        setError(
          reason instanceof Error ? reason.message : "Unable to load users",
        );
    }
  }
  useEffect(() => {
    void loadUsers();
  }, []);
  const visible = users.filter((user) =>
    `${user.name} ${user.email}`.toLowerCase().includes(query.toLowerCase()),
  );
  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    window.location.assign("/");
  }
  async function createUser(event: FormEvent) {
    event.preventDefault();
    setCreating(true);
    setDeleteError("");
    setMessage("");
    try {
      const result = await api<{ status: string; message?: string }>(
        "/api/admin/users",
        { method: "POST", body: JSON.stringify(draft) },
      );
      setDraft({ name: "", email: "", password: "" });
      setCreateOpen(false);
      setMessage(
        result.status === "pending"
          ? "User is pending group reconciliation."
          : "User created and added to the lab.",
      );
      await loadUsers();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to create user",
      );
    } finally {
      setCreating(false);
    }
  }
  async function deleteUser() {
    if (!pendingDelete) return;
    setError("");
    setMessage("");
    try {
      await api(`/api/admin/users/${encodeURIComponent(pendingDelete.id)}`, {
        method: "DELETE",
      });
      setPendingDelete(null);
      setMessage("User deleted from the lab.");
      await loadUsers();
    } catch (reason) {
      setDeleteError(
        reason instanceof ApiRequestError &&
          reason.status === 404 &&
          reason.message === "Not Found"
          ? "User deletion is unavailable on the deployed server. Update the AIDP Lab backend and try again."
          : reason instanceof Error
            ? reason.message
            : "Unable to delete user.",
      );
    }
  }
  return (
    <>
      <Shell adminLink={false} onSignOut={() => setLogoutOpen(true)}>
        <section className="admin">
          <div className="admin-panel">
            <div className="admin-toolbar">
              <form
                className="search"
                onSubmit={(event) => {
                  event.preventDefault();
                  setQuery(search);
                }}
              >
                <label>
                  <span className="sr-only">Search users</span>
                  <input
                    type="search"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Search by name or email"
                  />
                </label>
                <button
                  className="search-submit"
                  type="submit"
                  aria-label="Search users"
                  title="Search users"
                >
                  <SearchIcon />
                </button>
              </form>
              <div className="toolbar-actions">
                <button
                  className="toolbar-icon"
                  type="button"
                  onClick={() => void loadUsers()}
                  aria-label="Refresh users"
                  title="Refresh users"
                >
                  <RefreshIcon />
                </button>
                <button
                  className="create-user"
                  type="button"
                  onClick={() => {
                    setCreateOpen((current) => !current);
                    setError("");
                  }}
                >
                  <PlusIcon />
                  <span>User</span>
                </button>
              </div>
            </div>
            {createOpen && (
              <form className="admin-create" onSubmit={createUser}>
                <label>
                  Full name
                  <input
                    value={draft.name}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                    minLength={2}
                    maxLength={120}
                    required
                  />
                </label>
                <label>
                  Email
                  <input
                    type="email"
                    value={draft.email}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        email: event.target.value,
                      }))
                    }
                    required
                  />
                </label>
                <label>
                  Password
                  <input
                    type="password"
                    value={draft.password}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        password: event.target.value,
                      }))
                    }
                    minLength={8}
                    maxLength={256}
                    required
                  />
                </label>
                <div className="admin-form-actions">
                  <button
                    className="secondary"
                    type="button"
                    onClick={() => setCreateOpen(false)}
                  >
                    Cancel
                  </button>
                  <button disabled={creating}>
                    {creating ? "Creating…" : "Create user"}
                  </button>
                </div>
              </form>
            )}
            {error && (
              <p className="notice error" role="alert">
                {error}
              </p>
            )}
            {message && (
              <p className="notice success" role="status">
                {message}
              </p>
            )}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Status</th>
                    <th>Identity</th>
                    <th className="actions-column">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((user, index) => (
                    <tr key={user.id}>
                      <td>
                        <span className="row-index">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        {user.name}
                      </td>
                      <td>{user.email}</td>
                      <td>
                        <span className={`badge ${user.status}`}>
                          {user.status}
                        </span>
                      </td>
                      <td>
                        <span
                          className={`badge ${user.active ? "active" : "inactive"}`}
                        >
                          {user.active ? "Active" : "Inactive"}
                        </span>
                      </td>
                      <td className="row-actions">
                        <button
                          className="table-delete"
                          type="button"
                          onClick={() => {
                            setDeleteError("");
                            setPendingDelete(user);
                          }}
                          aria-label={`Delete ${user.email}`}
                          title="Delete"
                        >
                          <TrashIcon />
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!visible.length && (
                    <tr>
                      <td colSpan={5} className="empty">
                        No matching lab users.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </Shell>
      <ConfirmModal
        open={Boolean(pendingDelete)}
        kind="delete"
        title="Delete user?"
        description={`This will permanently remove ${pendingDelete?.email ?? "this user"} from Identity Domains.`}
        error={deleteError}
        confirmLabel="Delete"
        onClose={() => {
          setDeleteError("");
          setPendingDelete(null);
        }}
        onConfirm={() => void deleteUser()}
      />
      <ConfirmModal
        open={logoutOpen}
        kind="question"
        title="Log out?"
        description="You will need to sign in again to manage lab users."
        confirmLabel="Log out"
        onClose={() => setLogoutOpen(false)}
        onConfirm={() => void logout()}
      />
    </>
  );
}

function AdminSettings() {
  const [aidpUrl, setAidpUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    void api<{ aidp_url: string }>("/api/admin/settings")
      .then((result) => setAidpUrl(result.aidp_url))
      .catch((reason) => {
        if (reason instanceof ApiRequestError && reason.status === 401)
          window.location.assign("/admin/login");
        // ponytail: legacy deployed backends lack this route; use the OCI landing page until the exact deep link is available.
        else if (reason instanceof ApiRequestError && reason.status === 404)
          setAidpUrl("https://cloud.oracle.com/ai-data-platform/");
        else
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to load settings",
          );
      });
  }, []);
  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    window.location.assign("/");
  }
  return (
    <Shell adminLink={false} onSignOut={logout}>
      <section className="settings-page">
        <div className="settings-heading">
          <h1>Settings</h1>
          <p>Review the lab configuration.</p>
        </div>
        <div className="settings-surface">
          <div className="settings-tabs">
            <span>Application</span>
          </div>
          <div className="settings-intro">
            <span className="settings-icon">
              <AdminLoginIcon />
            </span>
            <div>
              <strong>AI Data Platform</strong>
              <p>Open the workspace configured for this lab.</p>
            </div>
          </div>
          <label className="settings-field">
            AI Data Platform URL
            <input
              value={aidpUrl}
              readOnly
              aria-label="AI Data Platform URL"
              placeholder="Loading configuration…"
            />
            {aidpUrl && (
              <a
                className="settings-link"
                href={aidpUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open AI Data Platform
              </a>
            )}
          </label>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>
    </Shell>
  );
}

export function App() {
  if (window.location.pathname === "/admin/settings") return <AdminSettings />;
  if (window.location.pathname === "/admin/login") return <AdminLogin />;
  if (window.location.pathname === "/admin/users") return <AdminUsers />;
  return <RegisterPage />;
}
