import assert from "node:assert/strict";
import test from "node:test";
import { detectFinalTransitions, extractStatuses, normalizeStatus } from "./worker.mjs";

test("in-progress to final dispatches once", () => {
  const r = detectFinalTransitions({ 1: "STATUS_IN_PROGRESS" }, { 1: "STATUS_FINAL" });
  assert.deepEqual(r.transitions, ["1"]);
  assert.equal(r.nextState["1"], "STATUS_FINAL");
});

test("final to final does not re-dispatch", () => {
  assert.deepEqual(detectFinalTransitions({ 1: "STATUS_FINAL" }, { 1: "STATUS_FINAL" }).transitions, []);
});

test("first sighting of a final game does not dispatch", () => {
  assert.deepEqual(detectFinalTransitions({}, { 1: "STATUS_FINAL" }).transitions, []);
  assert.deepEqual(detectFinalTransitions(null, { 1: "STATUS_FINAL" }).transitions, []);
});

test("scheduled to final does not dispatch", () => {
  assert.deepEqual(detectFinalTransitions({ 1: "STATUS_SCHEDULED" }, { 1: "STATUS_FINAL" }).transitions, []);
});

test("halftime and end-of-period count as live", () => {
  assert.equal(normalizeStatus("STATUS_HALFTIME"), "STATUS_IN_PROGRESS");
  assert.equal(normalizeStatus("STATUS_END_PERIOD"), "STATUS_IN_PROGRESS");
  const prev = extractStatuses({ events: [{ id: "9", status: { type: { name: "STATUS_END_PERIOD" } } }] });
  const cur = extractStatuses({ events: [{ id: "9", status: { type: { name: "STATUS_FINAL" } } }] });
  assert.deepEqual(detectFinalTransitions(prev, cur).transitions, ["9"]);
});

test("multiple games only dispatch the ones that finished", () => {
  const r = detectFinalTransitions(
    { 1: "STATUS_IN_PROGRESS", 2: "STATUS_IN_PROGRESS" },
    { 1: "STATUS_FINAL", 2: "STATUS_IN_PROGRESS" },
  );
  assert.deepEqual(r.transitions, ["1"]);
});
