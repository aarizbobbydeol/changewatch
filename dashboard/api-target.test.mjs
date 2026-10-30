import { test } from "node:test";
import assert from "node:assert/strict";
import target from "./api-target.js";

const { apiTarget } = target;

test("Docker and local runs keep the old default", () => {
  assert.equal(apiTarget({}), "http://localhost:8000");
  assert.equal(apiTarget({ API_URL: "http://api:8000" }), "http://api:8000");
});

test("NEXT_PUBLIC_API_URL wins, and trailing slashes would double up in the rewrites", () => {
  assert.equal(apiTarget({ API_URL: "http://a:1", NEXT_PUBLIC_API_URL: "http://b:2/" }), "http://b:2");
  assert.equal(apiTarget({ VERCEL: "1", API_URL: " https://cw.example.com// " }), "https://cw.example.com");
});

test("on Vercel the server address is required", () => {
  assert.throws(() => apiTarget({ VERCEL: "1" }), /Set API_URL to your ChangeWatch server's https address/);
});

test("on Vercel keys never go to the server over plain http", () => {
  assert.throws(() => apiTarget({ VERCEL: "1", API_URL: "http://cw.example.com" }), /must start with https:\/\//);
  assert.throws(() => apiTarget({ VERCEL: "1", API_URL: "https://" }), /must start with https:\/\//);
  assert.equal(apiTarget({ VERCEL: "1", API_URL: "https://cw.example.com" }), "https://cw.example.com");
});
