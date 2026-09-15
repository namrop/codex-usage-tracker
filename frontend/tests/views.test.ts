import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { DEFAULT_VIEW, VIEWS, parseViewHash, sameDocumentAnchorNavigation, viewForAnchor, viewHref } from "../src/lib/views";

const PAGE = "https://usage.example/app/#subscription-card-kimi-k3-coding";

describe("sameDocumentAnchorNavigation", () => {
  it("recognises a plain left click on a same-document anchor and says whether the hash repeats", () => {
    const repeat = sameDocumentAnchorNavigation({ href: "#subscription-card-kimi-k3-coding", currentHref: PAGE });
    assert.deepEqual(repeat, {
      hash: "#subscription-card-kimi-k3-coding",
      location: { view: "now", anchor: "subscription-card-kimi-k3-coding" },
      sameHash: true,
    });
    const other = sameDocumentAnchorNavigation({ href: "#subscription-card-anthropic", currentHref: PAGE });
    assert.equal(other?.sameHash, false);
    assert.equal(other?.location.anchor, "subscription-card-anthropic");
    // Absolute same-document hrefs count too (a copied link, a nav link).
    const absolute = sameDocumentAnchorNavigation({ href: "https://usage.example/app/#history", currentHref: PAGE });
    assert.deepEqual(absolute?.location, { view: "history", anchor: null });
    assert.equal(absolute?.sameHash, false);
  });

  it("leaves modified clicks, non-primary buttons, handled events, targets and downloads to the browser", () => {
    const facts = { href: "#subscription-card-kimi-k3-coding", currentHref: PAGE };
    assert.equal(sameDocumentAnchorNavigation({ ...facts, metaKey: true }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, ctrlKey: true }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, shiftKey: true }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, altKey: true }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, button: 1 }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, defaultPrevented: true }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, target: "_blank" }), null);
    assert.equal(sameDocumentAnchorNavigation({ ...facts, download: "" }), null);
    assert.notEqual(sameDocumentAnchorNavigation({ ...facts, target: "_self" }), null);
    assert.notEqual(sameDocumentAnchorNavigation({ ...facts, target: "" }), null);
  });

  it("ignores links that leave the document or carry no fragment", () => {
    assert.equal(sameDocumentAnchorNavigation({ href: "https://elsewhere.example/#history", currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: "/other/#history", currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: "?expand=all#history", currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: "/app/", currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: null, currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: "", currentHref: PAGE }), null);
    assert.equal(sameDocumentAnchorNavigation({ href: "not a url", currentHref: "not a url either" }), null);
  });
});

describe("parseViewHash", () => {
  it("names four real views and defaults to Now", () => {
    assert.deepEqual(VIEWS.map((view) => view.id), ["now", "history", "accounting", "diagnostics"]);
    assert.equal(DEFAULT_VIEW, "now");
    assert.deepEqual(parseViewHash(""), { view: "now", anchor: null });
    assert.deepEqual(parseViewHash("#"), { view: "now", anchor: null });
    assert.deepEqual(parseViewHash(undefined), { view: "now", anchor: null });
  });

  it("routes each view hash, case-insensitively", () => {
    assert.deepEqual(parseViewHash("#history"), { view: "history", anchor: null });
    assert.deepEqual(parseViewHash("#Accounting"), { view: "accounting", anchor: null });
    assert.deepEqual(parseViewHash("diagnostics"), { view: "diagnostics", anchor: null });
  });

  it("keeps the old reset deep links working: a provider card anchor opens Now with the anchor", () => {
    assert.deepEqual(parseViewHash("#subscription-card-anthropic"), {
      view: "now",
      anchor: "subscription-card-anthropic",
    });
    assert.deepEqual(parseViewHash("#subscription-card-z-ai-glm"), {
      view: "now",
      anchor: "subscription-card-z-ai-glm",
    });
  });

  it("sends known chart and table anchors to their views", () => {
    assert.equal(viewForAnchor("subscriptionUnifiedWeeklyChart"), "history");
    assert.equal(viewForAnchor("modelTokenChart"), "history");
    assert.equal(viewForAnchor("billingTitle"), "accounting");
    assert.equal(viewForAnchor("rulingsTitle"), "accounting");
    assert.equal(viewForAnchor("subscriptionsRawFoldBody"), "diagnostics");
    assert.equal(viewForAnchor("burnTitle"), "now");
    assert.equal(viewForAnchor("capacityTitle"), "now");
  });

  it("routes the weekly allowance deep link to History with the panel id as its anchor", () => {
    assert.deepEqual(parseViewHash("#weeklyUtilization"), { view: "history", anchor: "weeklyUtilization" });
    assert.equal(viewForAnchor("weeklyUtilizationTitle"), "history");
  });

  it("falls back to Now with no anchor for an unknown hash", () => {
    assert.deepEqual(parseViewHash("#nonsense"), { view: "now", anchor: null });
    assert.deepEqual(parseViewHash("#%E2%9C%93"), { view: "now", anchor: null });
  });

  it("builds the hrefs the nav links use", () => {
    assert.equal(viewHref("history"), "#history");
    assert.equal(viewHref(parseViewHash(viewHref("accounting")).view), "#accounting");
  });
});
