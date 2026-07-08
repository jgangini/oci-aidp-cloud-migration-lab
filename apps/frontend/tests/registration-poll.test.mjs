import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";

const frontendRoot = dirname(fileURLToPath(new URL("../package.json", import.meta.url)));
const output = await mkdtemp(join(tmpdir(), "aidp-registration-poll-"));
await promisify(execFile)(
  process.execPath,
  [
    join(frontendRoot, "node_modules", "typescript", "bin", "tsc"),
    join(frontendRoot, "src", "registrationPoll.ts"),
    "--target",
    "ES2022",
    "--module",
    "CommonJS",
    "--lib",
    "ES2022,DOM",
    "--strict",
    "--skipLibCheck",
    "--outDir",
    output,
  ],
  { cwd: frontendRoot },
);
const require = createRequire(import.meta.url);
const {
  ApiRequestError,
  getOrCreateResetOperation,
  loadResetOperation,
  persistResetOperation,
  RegistrationPollingTimeout,
  parseRetryAfter,
  pollRegistration,
  registrationProgress,
} = require(join(output, "registrationPoll.js"));

after(() => rm(output, { recursive: true, force: true }));

test("polling reconciles pending responses until active", async () => {
  const responses = [
    { status: "pending", phase: "schemas", message: "creating schemas" },
    { status: "active", aidp_url: "https://example.invalid/aidp" },
  ];
  const phases = [];
  const delays = [];
  const result = await pollRegistration({
    request: async () => responses.shift(),
    onPending: ({ phase }) => phases.push(phase),
    sleep: async (delay) => delays.push(delay),
    deadlineMs: 1_000,
  });
  assert.equal(result.status, "active");
  assert.deepEqual(phases, ["schemas"]);
  assert.deepEqual(delays, [2_000]);
});

test("registration progress counts completed provisioning phases", () => {
  assert.deepEqual(registrationProgress("identity"), {
    step: 1,
    total: 5,
    percent: 0,
  });
  assert.deepEqual(registrationProgress("cleanup"), {
    step: 1,
    total: 5,
    percent: 0,
  });
  assert.deepEqual(registrationProgress("schemas"), {
    step: 3,
    total: 5,
    percent: 40,
  });
  assert.deepEqual(registrationProgress("permissions"), {
    step: 5,
    total: 5,
    percent: 80,
  });
  assert.deepEqual(registrationProgress("future-phase"), {
    step: 1,
    total: 5,
    percent: 0,
  });
});

test("polling honors Retry-After and keeps reconciling after 429", async () => {
  let attempts = 0;
  const delays = [];
  const result = await pollRegistration({
    request: async () => {
      attempts += 1;
      if (attempts === 1) throw new ApiRequestError("limited", 429, 7_000);
      return { status: "active" };
    },
    sleep: async (delay) => delays.push(delay),
    deadlineMs: 1_000,
  });
  assert.equal(result.status, "active");
  assert.equal(attempts, 2);
  assert.deepEqual(delays, [7_000]);
  assert.equal(parseRetryAfter("7", 0), 7_000);
});

test("polling aborts an in-flight request at the deadline", async () => {
  const request = (signal) =>
    new Promise((_, reject) =>
      signal.addEventListener("abort", () => reject(signal.reason), { once: true }),
    );
  await assert.rejects(
    pollRegistration({ request, deadlineMs: 10 }),
    RegistrationPollingTimeout,
  );
});

test("polling propagates caller aborts without reporting a timeout", async () => {
  const controller = new AbortController();
  const request = (signal) =>
    new Promise((_, reject) =>
      signal.addEventListener("abort", () => reject(signal.reason), { once: true }),
    );
  const pending = pollRegistration({
    request,
    signal: controller.signal,
    deadlineMs: 1_000,
  });
  controller.abort(new DOMException("closed", "AbortError"));
  await assert.rejects(pending, (error) => {
    assert.equal(error.name, "AbortError");
    assert.notEqual(error.name, "RegistrationPollingTimeout");
    return true;
  });
});

test("polling preserves an immutable-industry 409", async () => {
  const conflict = new ApiRequestError("industry is immutable", 409);
  await assert.rejects(
    pollRegistration({
      request: async () => {
        throw conflict;
      },
      deadlineMs: 1_000,
    }),
    (error) => error === conflict && error.message === "industry is immutable",
  );
});

test("reset operations reuse one UUID until completion", () => {
  let created = 0;
  const createId = () => `operation-${++created}`;
  const first = getOrCreateResetOperation(undefined, "banking", createId);
  const retry = getOrCreateResetOperation(first, "banking", createId);

  assert.deepEqual(first, { industry: "banking", operationId: "operation-1" });
  assert.equal(retry, first);
  assert.equal(created, 1);
  assert.throws(
    () => getOrCreateResetOperation(first, "retail", createId),
    /Finish the pending AIDP reset/,
  );
  assert.equal(created, 1);
  assert.deepEqual(getOrCreateResetOperation(undefined, "retail", createId), {
    industry: "retail",
    operationId: "operation-2",
  });
});

test("reset operations survive a page reload until completion", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const operation = {
    industry: "healthcare",
    operationId: "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
  };

  persistResetOperation(storage, "user-1", operation);
  assert.deepEqual(loadResetOperation(storage, "user-1", ["healthcare"]), operation);
  persistResetOperation(storage, "user-1");
  assert.equal(loadResetOperation(storage, "user-1", ["healthcare"]), undefined);
  assert.doesNotThrow(() =>
    persistResetOperation({ ...storage, removeItem: () => { throw new Error("blocked"); } }, "user-1"),
  );
  storage.setItem(
    "aidp-lab.reset.user-1",
    JSON.stringify({ industry: "unknown", operationId: operation.operationId }),
  );
  assert.equal(loadResetOperation(storage, "user-1", ["healthcare"]), undefined);
});
