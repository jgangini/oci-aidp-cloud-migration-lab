import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const viteConfig = await readFile(new URL("../vite.config.ts", import.meta.url), "utf8");

test("secrets are never persisted in browser storage", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});

test("registration has no password field while administrator login remains protected", () => {
  assert.match(source, /type="password"/);
  assert.match(source, /Registration code/);
  assert.match(source, /function AdminLogin/);
  assert.match(source, /Industry/);
  assert.match(source, /value="banking"/);
  assert.doesNotMatch(source, /Generate password/);
});

test("registration code uses eight accessible segmented inputs", () => {
  assert.match(source, /className="code-slots"/);
  assert.match(source, /Registration code character \$\{index \+ 1\} of 8/);
  assert.match(source, /\^\[A-Z\]\{4\}-\[0-9\]\{4\}\$/);
});

test("development API proxy can target the deployed lab without committing a URL", () => {
  assert.match(viteConfig, /AIDP_API_PROXY_TARGET/);
  assert.match(viteConfig, /secure: false/);
  assert.match(viteConfig, /aidp_lab_admin_dev/);
});

test("administrator UI manages lab users through protected API routes", () => {
  assert.match(source, /\/api\/admin\/users/);
  assert.match(source, /Delete \$\{user\.email\}/);
  assert.match(source, /onSignOut=\{\(\) => setLogoutOpen\(true\)\}/);
  assert.match(source, /className="search-submit"/);
  assert.match(source, /<PlusIcon \/>/);
  assert.match(source, /<h1>Users<\/h1>/);
  assert.match(source, /<span>Users<\/span>/);
  assert.match(source, /data-tooltip="Logout"/);
  assert.match(source, /className="header-band"/);
  assert.match(source, /aria-label="Admin navigation"/);
  assert.match(source, /href="\/admin\/settings"/);
  assert.match(source, /currentPath === "\/admin\/users"/);
  assert.match(source, /title="Delete user\?"/);
  assert.match(source, /title="Log out\?"/);
  assert.match(source, /<th>Identity<\/th>/);
  assert.match(source, /user\.active \? "Active" : "Inactive"/);
  assert.match(source, /\/api\/admin\/settings/);
  assert.match(source, /\{tableError\} Refresh and try again\./);
  assert.match(source, /className="table-error"/);
  assert.match(source, /!tableError && !visible\.length/);
  assert.match(source, /Open AI Data Platform/);
  assert.match(source, /function Toast/);
  assert.match(source, /window\.setTimeout\(onDismiss, 4_000\)/);
  assert.match(source, /className="toast"/);
  assert.match(source, /className="toast-dismiss"/);
  assert.match(source, /aria-label="Dismiss notification"/);
  assert.match(source, /function CopyIcon/);
  assert.match(source, /navigator\.clipboard\.writeText\(aidpUrl\)/);
  assert.match(source, /className="settings-url-control"/);
  assert.match(source, /aria-label="Copy AI Data Platform URL"/);
  assert.match(source, /className="confirm-error"/);
});

test("registration waits for OCI reconciliation and only then exposes the AIDP link", () => {
  assert.match(source, /result\.status !== "pending"/);
  assert.match(source, /<p>\{state\.message\}<\/p>/);
  assert.match(source, /Open AI Data Platform/);
  assert.match(source, /function AccessReadyIcon/);
  assert.match(source, /aria-labelledby="registration-ready-title"/);
  assert.match(source, /className="registration-result registration-result-ready"/);
});
