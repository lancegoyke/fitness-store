// usePlanSync (#709 PR 2) — the designer's live-sync poll. Asks
// `GET /meso/api/plan/<id>/sync/?v=<stamp>&mesocycle=<id>` and hands a changed
// grid to `onGrid` (useGrid.applyRemoteGrid), which owns every merge rule.
//
// Cadence: 3s while "active" (a local write or a remote change in the last 2
// minutes); idle it backs off 3 -> 6 -> 12 -> 30s. Only while the tab is
// visible; polls at once on visible and on window focus; never two in flight.
// 404/403 (old replica mid-deploy, plan gone) back off to 60s; 10 in a row
// stop it for good with one console.warn. A network or 5xx error backs off the same ladder, silently
// (the write path surfaces failed writes). No known stamp -> no polling.
import { useCallback, useEffect, useRef } from "react";
import type { MesoGrid } from "../lib/api";
import type { Id } from "./useGrid";

const STEPS_MS = [3000, 6000, 12000, 30000];
const ACTIVE_MS = 2 * 60 * 1000;
const FAST_MS = 3000;
const MISSING_MS = 60000;
const MISSING_STOP = 10;

export interface UsePlanSyncOptions {
  planId: Id;
  mesocycleId?: Id;
  /** The stamp of the grid on screen (undefined: unknown, do not poll). */
  getVersion: () => number | undefined;
  /** Merge a changed grid; true when the coach-visible grid changed. */
  onGrid: (grid: MesoGrid) => boolean;
}

export function usePlanSync(options: UsePlanSyncOptions) {
  const { planId, mesocycleId } = options;
  const optsRef = useRef(options);
  optsRef.current = options;
  const markRef = useRef<() => void>(() => {});
  const markActive = useCallback(() => markRef.current(), []);

  useEffect(() => {
    let stopped = false;
    let inflight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let timerDue = 0;
    let lastActive = 0;
    let idleIdx = 0;
    let errIdx = 0;
    let missing = 0; // consecutive 404/403s
    const visible = () => document.visibilityState !== "hidden";

    const clear = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const arm = (delay: number) => {
      clear();
      timerDue = Date.now() + delay;
      timer = setTimeout(() => {
        timer = null;
        void poll();
      }, delay);
    };
    const schedule = () => {
      if (stopped || !visible()) return;
      let delay: number;
      if (missing > 0) delay = MISSING_MS;
      else if (errIdx > 0) delay = STEPS_MS[Math.min(errIdx - 1, STEPS_MS.length - 1)]!;
      else if (Date.now() - lastActive < ACTIVE_MS) {
        idleIdx = 0;
        delay = FAST_MS;
      } else {
        delay = STEPS_MS[Math.min(idleIdx, STEPS_MS.length - 1)]!;
        idleIdx++;
      }
      arm(delay);
    };
    const markActiveNow = () => {
      lastActive = Date.now();
      idleIdx = 0;
      // Snap a long idle wait back to the fast cadence.
      if (timer && !inflight && timerDue - Date.now() > FAST_MS) arm(FAST_MS);
    };
    markRef.current = markActiveNow;

    async function poll() {
      if (stopped || inflight || !visible()) return;
      const v = optsRef.current.getVersion();
      if (v === undefined) {
        schedule();
        return;
      }
      clear();
      inflight = true;
      try {
        const q = mesocycleId !== undefined && mesocycleId !== "" ? `&mesocycle=${mesocycleId}` : "";
        const res = await fetch(`/meso/api/plan/${planId}/sync/?v=${v}${q}`);
        if (stopped) return;
        if (res.status === 404 || res.status === 403) {
          // An old replica mid-deploy answers 404 too: back off and retry,
          // and give up only after MISSING_STOP in a row.
          missing++;
          if (missing >= MISSING_STOP) {
            stopped = true;
            console.warn("Live sync stopped:", res.status);
          }
          return;
        }
        missing = 0;
        if (!res.ok) throw new Error("sync " + res.status);
        const data = (await res.json()) as { changed?: boolean; grid?: MesoGrid };
        if (stopped) return;
        errIdx = 0;
        if (data.changed && data.grid && optsRef.current.onGrid(data.grid)) markActiveNow();
      } catch {
        if (!stopped) errIdx++;
      } finally {
        inflight = false;
        if (!stopped) schedule();
      }
    }

    const pollNow = () => {
      if (stopped || inflight || !visible()) return;
      clear();
      void poll();
    };
    const onVisibility = () => {
      if (visible()) pollNow();
      else clear();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", pollNow);
    schedule();
    return () => {
      stopped = true;
      clear();
      markRef.current = () => {};
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", pollNow);
    };
  }, [planId, mesocycleId]);

  return { markActive };
}
