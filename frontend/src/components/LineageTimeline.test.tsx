import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LineageTimeline } from "./LineageTimeline";

describe("undo recovery control", () => {
  it("keeps recovery available even when the refreshed history has no undoable entries", () => {
    const markup = renderToStaticMarkup(<LineageTimeline history={[]} sourceName="orders.csv" live canUndo={false} busy={false} undoNeedsRecovery onUndo={() => undefined} />);
    expect(markup).toContain("Retry undo");
    expect(markup).not.toContain("disabled");
  });

  it("disables recovery while the previous request is in progress", () => {
    const markup = renderToStaticMarkup(<LineageTimeline history={[]} sourceName="orders.csv" live canUndo={false} busy undoNeedsRecovery onUndo={() => undefined} />);
    expect(markup).toContain("Retry undo");
    expect(markup).toContain("disabled");
  });
});
