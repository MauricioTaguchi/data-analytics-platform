import { isAmbiguousJobCreationError, undoDatasetTransformation, type Dataset, type UndoReceipt } from "../api-client";

type UndoIntent = {
  expectedVersion: number;
  key: string;
  receipt: UndoReceipt | null;
  synchronized: boolean;
  pending: Promise<UndoReceipt> | null;
};

type UndoDependencies = {
  execute?: typeof undoDatasetTransformation;
  createKey?: () => string;
};

export function createUndoCoordinator({ execute = undoDatasetTransformation, createKey = () => crypto.randomUUID() }: UndoDependencies = {}) {
  const intents = new Map<number, UndoIntent>();
  let generation = 0;

  function run(dataset: Pick<Dataset, "id" | "version">, synchronize: () => Promise<void>): Promise<UndoReceipt> {
    let intent = intents.get(dataset.id);
    if (!intent || (intent.synchronized && dataset.version !== intent.expectedVersion)) {
      intent = { expectedVersion: dataset.version, key: createKey(), receipt: null, synchronized: false, pending: null };
      intents.set(dataset.id, intent);
    }
    if (intent.pending) return intent.pending;
    if (intent.synchronized && intent.receipt) return Promise.resolve(intent.receipt);

    const currentIntent = intent;
    const startedGeneration = generation;
    currentIntent.pending = (async () => {
      if (!currentIntent.receipt) {
        try {
          currentIntent.receipt = await execute(dataset.id, currentIntent.expectedVersion, currentIntent.key);
        } catch (error) {
          // An uncertain response must keep the original key and observed revision.
          if (!isAmbiguousJobCreationError(error) && intents.get(dataset.id) === currentIntent) intents.delete(dataset.id);
          throw error;
        }
      }
      if (startedGeneration !== generation) throw new DOMException("Request cancelled", "AbortError");
      // Keep the receipt until both views refresh: another click must not undo twice.
      await synchronize();
      currentIntent.synchronized = true;
      return currentIntent.receipt;
    })().finally(() => { currentIntent.pending = null; });
    return currentIntent.pending;
  }

  function reset() {
    generation += 1;
    intents.clear();
  }

  function needsRecovery(datasetId: number) {
    const intent = intents.get(datasetId);
    return Boolean(intent && !intent.synchronized);
  }

  return { run, reset, needsRecovery };
}
