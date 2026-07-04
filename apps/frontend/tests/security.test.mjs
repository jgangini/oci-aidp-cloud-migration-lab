import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

test("secrets are never persisted in browser storage", () => {
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});

test("registration and administrator forms use password inputs", () => {
  assert.match(source, /name="password"|type="password"/);
  assert.match(source, /Registration code/);
  assert.match(source, /Administrator login/);
});
