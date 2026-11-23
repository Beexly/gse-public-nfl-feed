// Cloudflare Worker (cron-triggered): watches the public ESPN NFL scoreboard and, ONLY when a
// game moves from STATUS_IN_PROGRESS to STATUS_FINAL, fires a GitHub repository_dispatch
// ("nfl-game-final") so the postgame feed workflow runs once per completed game.
//
// Bindings (configured in Cloudflare, never in this repo):
//   NFL_STATE              KV namespace holding the last seen status per event
//   GITHUB_DISPATCH_TOKEN  Worker secret (fine-grained token, contents/dispatch scope on this repo)

export const ESPN_SCOREBOARD_URL =
  "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard";
export const DISPATCH_URL =
  "https://api.github.com/repos/Beexly/gse-public-nfl-feed/dispatches";
export const DISPATCH_EVENT_TYPE = "nfl-game-final";
export const STATE_KEY = "event_status_v1";

// Other in-game states (halftime, end of period, delays) are the same live game; they are
// folded into STATUS_IN_PROGRESS so a poll that lands between periods cannot hide the transition.
const LIVE_STATUSES = new Set([
  "STATUS_IN_PROGRESS",
  "STATUS_HALFTIME",
  "STATUS_END_PERIOD",
  "STATUS_DELAYED",
]);

export function normalizeStatus(name) {
  if (typeof name !== "string" || name === "") return "STATUS_UNKNOWN";
  return LIVE_STATUSES.has(name) ? "STATUS_IN_PROGRESS" : name;
}

export function extractStatuses(scoreboard) {
  const out = {};
  const events = Array.isArray(scoreboard?.events) ? scoreboard.events : [];
  for (const ev of events) {
    const id = ev?.id;
    if (id === undefined || id === null) continue;
    out[String(id)] = normalizeStatus(ev?.status?.type?.name);
  }
  return out;
}

// Pure transition logic. Returns the event ids that just finished and the next stored state.
export function detectFinalTransitions(prevState, currentStatuses) {
  const prev = prevState && typeof prevState === "object" ? prevState : {};
  const transitions = [];
  for (const [id, cur] of Object.entries(currentStatuses)) {
    if (prev[id] === "STATUS_IN_PROGRESS" && cur === "STATUS_FINAL") {
      transitions.push(id);
    }
  }
  return { transitions, nextState: { ...currentStatuses } };
}

async function sendDispatch(env, transitions) {
  const resp = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "gse-espn-final-dispatcher",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ event_type: DISPATCH_EVENT_TYPE }),
  });
  if (resp.status !== 204) {
    throw new Error(`repository_dispatch failed: HTTP ${resp.status} for events ${transitions.join(",")}`);
  }
}

export async function runOnce(env, fetchImpl = fetch) {
  const resp = await fetchImpl(ESPN_SCOREBOARD_URL, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`ESPN scoreboard HTTP ${resp.status}`);
  const current = extractStatuses(await resp.json());
  const prev = (await env.NFL_STATE.get(STATE_KEY, "json")) || {};
  const { transitions, nextState } = detectFinalTransitions(prev, current);

  if (transitions.length > 0) {
    try {
      if (!env.GITHUB_DISPATCH_TOKEN) throw new Error("GITHUB_DISPATCH_TOKEN secret not bound");
      await sendDispatch(env, transitions);
    } catch (err) {
      // Keep the prior in-progress status so the next poll retries the dispatch.
      for (const id of transitions) nextState[id] = prev[id];
      await env.NFL_STATE.put(STATE_KEY, JSON.stringify(nextState));
      throw err;
    }
  }
  if (JSON.stringify(nextState) !== JSON.stringify(prev)) {
    await env.NFL_STATE.put(STATE_KEY, JSON.stringify(nextState));
  }
  return { transitions };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runOnce(env));
  },
};
