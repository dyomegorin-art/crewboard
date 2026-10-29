// Крошечная шина для глобальных UI-событий (тосты и модалка «Перейдите на Pro»),
// чтобы mutation onError не тащил за собой контексты.
type Toast = { id: number; text: string; kind: "info" | "error" };
type UpgradePrompt = { code: string; message: string };

let toastId = 0;
const toastListeners = new Set<(t: Toast) => void>();
const upgradeListeners = new Set<(u: UpgradePrompt) => void>();

export const toast = (text: string, kind: Toast["kind"] = "info"): void =>
  toastListeners.forEach((fn) => fn({ id: ++toastId, text, kind }));
export const promptUpgrade = (u: UpgradePrompt): void => upgradeListeners.forEach((fn) => fn(u));

export function subscribeToasts(fn: (t: Toast) => void): () => void {
  toastListeners.add(fn);
  return () => toastListeners.delete(fn);
}
export function subscribeUpgrade(fn: (u: UpgradePrompt) => void): () => void {
  upgradeListeners.add(fn);
  return () => upgradeListeners.delete(fn);
}
export type { Toast, UpgradePrompt };
