export interface LibrarySession {
  readonly generation: number;
  isCurrent: (generation: number) => boolean;
  runMutation: <T>(generation: number, operation: () => Promise<T>) => Promise<T | null>;
  suspendAndDrain: () => Promise<void>;
  resume: () => void;
  invalidate: () => void;
  subscribe: (listener: () => void) => () => void;
}

export function createLibrarySession(): LibrarySession {
  let generation = 0;
  let suspended = false;
  const pending = new Set<Promise<unknown>>();
  const listeners = new Set<() => void>();
  return {
    get generation() { return generation; },
    isCurrent: (expected) => !suspended && expected === generation,
    runMutation: (expected, operation) => {
      if (suspended || expected !== generation) return Promise.resolve(null);
      const tracked = operation().finally(() => pending.delete(tracked));
      pending.add(tracked);
      return tracked;
    },
    suspendAndDrain: async () => {
      suspended = true;
      await Promise.allSettled([...pending]);
    },
    resume: () => { suspended = false; },
    invalidate: () => {
      generation += 1;
      for (const listener of listeners) listener();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}
