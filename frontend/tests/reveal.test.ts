import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { scheduleReveal, type RevealCandidate } from "../src/lib/reveal";

// A fake frame scheduler: frames run when the test says so, so the reveal
// loop's waiting, retrying and giving up are observable without a browser.
function fakeFrames() {
  const queue: Array<{ id: number; cb: () => void }> = [];
  let nextId = 1;
  return {
    frame: (cb: () => void) => {
      const id = nextId++;
      queue.push({ id, cb });
      return id;
    },
    cancel: (id: number) => {
      const at = queue.findIndex((entry) => entry.id === id);
      if (at !== -1) queue.splice(at, 1);
    },
    tick: () => {
      const entry = queue.shift();
      if (entry) entry.cb();
    },
    pending: () => queue.length,
  };
}

function candidate(options: { focusSticks?: boolean; revealed?: () => boolean } = {}) {
  const counts = { scrolls: 0, focuses: 0 };
  let focused = false;
  const target: RevealCandidate = {
    get revealed() {
      return options.revealed ? options.revealed() : true;
    },
    scrollIntoView: () => {
      counts.scrolls += 1;
    },
    focus: () => {
      counts.focuses += 1;
      if (options.focusSticks !== false) focused = true;
    },
    focused: () => focused,
  };
  return { target, counts };
}

describe("scheduleReveal", () => {
  it("scrolls to and focuses a revealed target, then stops", () => {
    const frames = fakeFrames();
    const { target, counts } = candidate();
    scheduleReveal(() => target, frames);
    frames.tick();
    assert.equal(counts.scrolls, 1);
    assert.equal(counts.focuses, 1);
    assert.equal(frames.pending(), 0, "done: no further frames");
  });

  it("waits across frames for a target that is not rendered or still hidden, and reveals it once it is", () => {
    const frames = fakeFrames();
    let present = false;
    let hidden = true;
    const { target, counts } = candidate({ revealed: () => !hidden });
    scheduleReveal(() => (present ? target : null), frames);
    frames.tick();
    frames.tick();
    assert.equal(counts.focuses, 0, "nothing to reveal yet");
    present = true;
    frames.tick();
    assert.equal(counts.focuses, 0, "present but hidden inside a closed fold");
    hidden = false;
    frames.tick();
    assert.equal(counts.scrolls, 1);
    assert.equal(counts.focuses, 1);
    assert.equal(frames.pending(), 0);
  });

  it("retries focus when it did not land, and gives up after the attempt budget", () => {
    const frames = fakeFrames();
    const { target, counts } = candidate({ focusSticks: false });
    scheduleReveal(() => target, frames, 4);
    for (let i = 0; i < 10; i += 1) frames.tick();
    assert.equal(counts.focuses, 4, "one attempt per frame up to the budget");
    assert.equal(frames.pending(), 0, "gave up: no frame left queued");
  });

  it("returns a canceller that stops the loop", () => {
    const frames = fakeFrames();
    const { target, counts } = candidate();
    let present = false;
    const cancel = scheduleReveal(() => (present ? target : null), frames);
    frames.tick();
    assert.equal(frames.pending(), 1);
    cancel();
    assert.equal(frames.pending(), 0);
    present = true;
    frames.tick();
    assert.equal(counts.focuses, 0);
  });
});
