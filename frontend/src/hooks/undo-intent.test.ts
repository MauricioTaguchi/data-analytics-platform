import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { describe, expect, it, vi } from "vitest";

import type { UndoReceipt } from "../api-client";
import { createUndoCoordinator } from "./undo-intent";

const receipt: UndoReceipt = {
  id: 12,
  operation: "drop_duplicates",
  parameters: {},
  before_rows: 3,
  after_rows: 2,
  before_columns: 1,
  after_columns: 1,
  status: "undone",
  task_id: "transform-task",
  expected_version: 7,
  undone_at: "2026-10-13T12:00:00Z",
  created_at: "2026-10-13T11:00:00Z",
  event_id: "undo-event",
  previous_version: 8,
  result_version: 9,
};

describe("undo intent coordination", () => {
  it("coalesces concurrent clicks into one mutation and one refresh", async () => {
    let resolve!: (value: UndoReceipt) => void;
    const execute = vi.fn(() => new Promise<UndoReceipt>((done) => { resolve = done; }));
    const synchronize = vi.fn().mockResolvedValue(undefined);
    const coordinator = createUndoCoordinator({ execute, createKey: () => "undo-1" });

    const first = coordinator.run({ id: 7, version: 8 }, synchronize);
    const second = coordinator.run({ id: 7, version: 8 }, synchronize);
    expect(second).toBe(first);
    resolve(receipt);
    await expect(first).resolves.toEqual(receipt);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith(7, 8, "undo-1");
    expect(synchronize).toHaveBeenCalledTimes(1);
  });

  it("preserves the original key and revision after an uncertain request", async () => {
    const execute = vi.fn()
      .mockRejectedValueOnce(new AxiosError("Response lost", "ERR_NETWORK"))
      .mockResolvedValue(receipt);
    const createKey = vi.fn().mockReturnValue("undo-1");
    const synchronize = vi.fn().mockResolvedValue(undefined);
    const coordinator = createUndoCoordinator({ execute, createKey });

    await expect(coordinator.run({ id: 7, version: 8 }, synchronize)).rejects.toThrow("Response lost");
    expect(coordinator.needsRecovery(7)).toBe(true);
    // A background refresh can already observe the committed server revision.
    await expect(coordinator.run({ id: 7, version: 9 }, synchronize)).resolves.toEqual(receipt);

    expect(execute.mock.calls).toEqual([[7, 8, "undo-1"], [7, 8, "undo-1"]]);
    expect(createKey).toHaveBeenCalledTimes(1);
    expect(synchronize).toHaveBeenCalledTimes(1);
    expect(coordinator.needsRecovery(7)).toBe(false);
  });

  it("retries only synchronization after the mutation committed and refresh failed", async () => {
    const execute = vi.fn().mockResolvedValue(receipt);
    const synchronize = vi.fn()
      .mockRejectedValueOnce(new Error("History is unavailable"))
      .mockResolvedValue(undefined);
    const createKey = vi.fn().mockReturnValueOnce("undo-1").mockReturnValueOnce("undo-2");
    const coordinator = createUndoCoordinator({ execute, createKey });

    await expect(coordinator.run({ id: 7, version: 8 }, synchronize)).rejects.toThrow("History is unavailable");
    expect(coordinator.needsRecovery(7)).toBe(true);
    await expect(coordinator.run({ id: 7, version: 9 }, synchronize)).resolves.toEqual(receipt);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute).toHaveBeenCalledWith(7, 8, "undo-1");
    expect(synchronize).toHaveBeenCalledTimes(2);

    // Once the prior intent is fully acknowledged, an explicit next undo is distinct.
    await coordinator.run({ id: 7, version: 9 }, synchronize);
    expect(execute).toHaveBeenLastCalledWith(7, 9, "undo-2");
  });

  it("does not submit another mutation while the displayed revision is stale", async () => {
    const execute = vi.fn().mockResolvedValue(receipt);
    const synchronize = vi.fn().mockResolvedValue(undefined);
    const coordinator = createUndoCoordinator({ execute, createKey: () => "undo-1" });

    await coordinator.run({ id: 7, version: 8 }, synchronize);
    await expect(coordinator.run({ id: 7, version: 8 }, synchronize)).resolves.toEqual(receipt);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(synchronize).toHaveBeenCalledTimes(1);
  });

  it("permits a new intention after a definitive revision conflict", async () => {
    const config = { headers: {} } as InternalAxiosRequestConfig;
    const conflict = { status: 409, config } as AxiosResponse;
    const execute = vi.fn()
      .mockRejectedValueOnce(new AxiosError("Revision changed", "ERR_BAD_REQUEST", config, undefined, conflict))
      .mockResolvedValue(receipt);
    const createKey = vi.fn().mockReturnValueOnce("undo-1").mockReturnValueOnce("undo-2");
    const coordinator = createUndoCoordinator({ execute, createKey });
    const synchronize = vi.fn().mockResolvedValue(undefined);

    await expect(coordinator.run({ id: 7, version: 8 }, synchronize)).rejects.toThrow("Revision changed");
    expect(coordinator.needsRecovery(7)).toBe(false);
    await coordinator.run({ id: 7, version: 10 }, synchronize);
    expect(execute).toHaveBeenLastCalledWith(7, 10, "undo-2");
  });

  it("keeps dataset intentions isolated", async () => {
    const execute = vi.fn().mockResolvedValue(receipt);
    const createKey = vi.fn().mockReturnValueOnce("dataset-7").mockReturnValueOnce("dataset-8");
    const synchronize = vi.fn().mockResolvedValue(undefined);
    const coordinator = createUndoCoordinator({ execute, createKey });

    await coordinator.run({ id: 7, version: 8 }, synchronize);
    await coordinator.run({ id: 8, version: 8 }, synchronize);
    expect(execute.mock.calls).toEqual([[7, 8, "dataset-7"], [8, 8, "dataset-8"]]);
  });

  it("does not refresh a former workspace after its state is reset", async () => {
    let resolve!: (value: UndoReceipt) => void;
    const execute = vi.fn(() => new Promise<UndoReceipt>((done) => { resolve = done; }));
    const synchronize = vi.fn().mockResolvedValue(undefined);
    const coordinator = createUndoCoordinator({ execute, createKey: () => "undo-1" });

    const pending = coordinator.run({ id: 7, version: 8 }, synchronize);
    coordinator.reset();
    resolve(receipt);
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(synchronize).not.toHaveBeenCalled();
  });
});
