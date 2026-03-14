import fs from "node:fs";
import { randomUUID } from "node:crypto";

const SESSION_TTL_MS = 10 * 60 * 1000;

const globalKey = "__swift_convert_sessions__";
const state = globalThis[globalKey] || {
  sessions: new Map(),
  cleanupStarted: false,
};

globalThis[globalKey] = state;

function cleanupExpired() {
  const now = Date.now();
  for (const [sessionId, meta] of state.sessions.entries()) {
    if (now - meta.createdAt > SESSION_TTL_MS) {
      state.sessions.delete(sessionId);
      if (meta.dir) {
        fs.rmSync(meta.dir, { recursive: true, force: true });
      }
    }
  }
}

if (!state.cleanupStarted) {
  state.cleanupStarted = true;
  setInterval(cleanupExpired, 60 * 1000).unref();
}

export function createSession(dir, extra = {}) {
  const sessionId = randomUUID().replace(/-/g, "");
  state.sessions.set(sessionId, {
    dir,
    createdAt: Date.now(),
    ...extra,
  });
  return sessionId;
}

export function getSession(sessionId) {
  const meta = state.sessions.get(sessionId);
  if (!meta) {
    return null;
  }
  if (Date.now() - meta.createdAt > SESSION_TTL_MS) {
    state.sessions.delete(sessionId);
    if (meta.dir) {
      fs.rmSync(meta.dir, { recursive: true, force: true });
    }
    return null;
  }
  return meta;
}
