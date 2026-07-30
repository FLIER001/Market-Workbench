import { useSyncExternalStore } from "react";
import { storageGet, storageSet } from "@/lib/storage";

export type BackgroundTaskStatus = "idle" | "running" | "done" | "error" | "cancelled";

export interface BackgroundTask<T> {
  status: BackgroundTaskStatus;
  data: T;
  error?: string;
  updatedAt: number;
}

type Updater<T> = (current: T) => T;
type Runner<T> = (update: (updater: Updater<T>) => void, signal: AbortSignal) => Promise<void>;

const STORAGE_KEY = "vr-background-ai-tasks";
const records = new Map<string, BackgroundTask<unknown>>();
const listeners = new Map<string, Set<() => void>>();
const controllers = new Map<string, AbortController>();

function loadSaved(): Record<string, BackgroundTask<unknown>> {
  try {
    return JSON.parse(storageGet(STORAGE_KEY) || "{}") as Record<string, BackgroundTask<unknown>>;
  } catch {
    return {};
  }
}

const saved = loadSaved();
for (const [key, task] of Object.entries(saved)) {
  records.set(key, task.status === "running"
    ? { ...task, status: "error", error: "页面曾在任务运行时被刷新，请重新发起", updatedAt: Date.now() }
    : task);
}

function persist() {
  storageSet(STORAGE_KEY, JSON.stringify(Object.fromEntries(records)));
}

function emit(key: string) {
  listeners.get(key)?.forEach((listener) => listener());
}

function setTask<T>(key: string, task: BackgroundTask<T>) {
  records.set(key, task as BackgroundTask<unknown>);
  persist();
  emit(key);
}

function ensureTask<T>(key: string, initialData: T): BackgroundTask<T> {
  const existing = records.get(key) as BackgroundTask<T> | undefined;
  if (existing) return existing;
  const created: BackgroundTask<T> = { status: "idle", data: initialData, updatedAt: Date.now() };
  records.set(key, created as BackgroundTask<unknown>);
  return created;
}

export function useBackgroundTask<T>(key: string, initialData: T): BackgroundTask<T> {
  ensureTask(key, initialData);
  return useSyncExternalStore(
    (listener) => {
      const set = listeners.get(key) || new Set<() => void>();
      set.add(listener);
      listeners.set(key, set);
      return () => {
        set.delete(listener);
        if (!set.size) listeners.delete(key);
      };
    },
    () => records.get(key) as BackgroundTask<T>,
    () => records.get(key) as BackgroundTask<T>,
  );
}

export function startBackgroundTask<T>(key: string, initialData: T, runner: Runner<T>): boolean {
  const current = ensureTask(key, initialData);
  if (current.status === "running") return false;

  const controller = new AbortController();
  controllers.set(key, controller);
  setTask(key, { status: "running", data: initialData, updatedAt: Date.now() });

  const update = (updater: Updater<T>) => {
    const latest = records.get(key) as BackgroundTask<T> | undefined;
    if (!latest || latest.status !== "running" || controllers.get(key) !== controller) return;
    setTask(key, { ...latest, data: updater(latest.data), updatedAt: Date.now() });
  };

  void runner(update, controller.signal)
    .then(() => {
      const latest = records.get(key) as BackgroundTask<T> | undefined;
      if (latest && controllers.get(key) === controller) {
        setTask(key, { ...latest, status: "done", error: undefined, updatedAt: Date.now() });
      }
    })
    .catch((error: unknown) => {
      const latest = records.get(key) as BackgroundTask<T> | undefined;
      if (!latest || controllers.get(key) !== controller) return;
      const aborted = controller.signal.aborted;
      const message = error instanceof Error ? error.message : "AI 任务失败";
      setTask(key, {
        ...latest,
        status: aborted ? "cancelled" : "error",
        error: aborted ? "任务已中止" : message,
        updatedAt: Date.now(),
      });
    })
    .finally(() => {
      if (controllers.get(key) === controller) controllers.delete(key);
    });
  return true;
}

export function updateBackgroundTask<T>(key: string, initialData: T, updater: Updater<T>) {
  const current = ensureTask(key, initialData);
  setTask(key, { ...current, data: updater(current.data), updatedAt: Date.now() });
}

export function cancelBackgroundTask(key: string) {
  const controller = controllers.get(key);
  if (!controller) return;
  controller.abort();
  controllers.delete(key);
  const latest = records.get(key);
  if (latest?.status === "running") {
    setTask(key, {
      ...latest,
      status: "cancelled",
      error: "任务已中止",
      updatedAt: Date.now(),
    });
  }
}

export function backgroundTaskKey(scope: string, identity: string): string {
  let hash = 2166136261;
  for (let i = 0; i < identity.length; i += 1) {
    hash ^= identity.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `${scope}:${(hash >>> 0).toString(36)}`;
}
