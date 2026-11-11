import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DatasetWorkspace } from "./components/DatasetWorkspace";
import { processingState } from "./processing-state";

const job = (status: string, progress = 100) => ({ task_id: "job-1", status, progress });

describe("processing status", () => {
  it.each([
    ["FAILURE", "failure", "Processing failed"],
    ["CANCELLED", "cancelled", "Processing cancelled"],
    ["REVOKED", "cancelled", "Processing cancelled"],
    ["TIMEOUT", "monitoring_unavailable", "Result not confirmed"],
    ["MONITORING_ERROR", "monitoring_unavailable", "Result not confirmed"],
  ] as const)("does not present %s as completed even if the backend progress is 100", (status, expectedStatus, title) => {
    const state = processingState([job(status)], "idle", 0, "idle");
    expect(state.status).toBe(expectedStatus);
    const html = renderToStaticMarkup(<DatasetWorkspace rows={[]} columns={[]} live processing={state} />);
    expect(html).toContain(title);
    expect(html).not.toContain("100%");
    expect(html).not.toContain(">Complete<");
    expect(html).not.toContain('role="progressbar"');
  });

  it("distinguishes idle, file upload, worker execution, and success", () => {
    expect(processingState([], "idle", 0, "idle")).toMatchObject({ status: "idle" });
    expect(processingState([], "uploading", 100, "running")).toMatchObject({ status: "running", progress: 99 });
    expect(processingState([job("STARTED", 43)], "processing", 100, "running")).toMatchObject({ status: "running", progress: 43 });
    expect(processingState([job("SUCCESS")], "idle", 0, "success")).toMatchObject({ status: "success", progress: 100 });
  });

  it("keeps an unknown worker outcome distinct from a failed upload request", () => {
    expect(processingState([job("MONITORING_ERROR")], "idle", 0, "failure").status).toBe("monitoring_unavailable");
    expect(processingState([job("SUCCESS")], "idle", 0, "failure").status).toBe("failure");
  });

  it("normalizes nonfinite running progress without implying completion", () => {
    expect(processingState([job("STARTED", NaN)], "idle", 0, "idle").progress).toBe(0);
    expect(processingState([job("STARTED", 100)], "idle", 0, "idle").progress).toBe(99);
  });
});
