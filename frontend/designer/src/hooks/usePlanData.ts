// usePlanData — sole owner of program/weeks/phases (CONTRACT.md "usePlanData").
// Ported from createMeso()'s applyPlanData/addExercise/addDay/switchWeek/
// addWeek/setCurrentWeek (app/store_project/static/js/meso.js). No `live`
// fixture branch here (Non-goals) — every verb always talks to the real API.
import { useCallback, useMemo, useState } from "react";
import { apiPost, EMPTY_HISTORY } from "../lib/api";
import { deliverHref as buildDeliverHref } from "../lib/deliver";
import type {
  AthleteIdentity,
  Day,
  Exercise,
  GroupIdentity,
  HistoryCarrier,
  HistoryState,
  Phase,
  PlanEnvelope,
  Week,
} from "../lib/api";

export type { Day, Exercise, Week, Phase } from "../lib/api";

/** Server ids are numeric; a couple of fixture-era tests used string ids. */
export type Id = number | string;

/** The confirm-armed day/week removal (CONTRACT.md "usePlanData"). */
export type PendingDelete = { type: "day"; di: number } | { type: "week"; weekId: Id };

export interface UsePlanDataInitial {
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: Id | null;
  history?: HistoryState;
}

export interface UsePlanDataIdentity {
  athlete: AthleteIdentity | null;
  group: GroupIdentity | null;
}

export function usePlanData(
  planId: Id,
  csrf: string,
  initial: UsePlanDataInitial,
  identity: UsePlanDataIdentity,
) {
  const [program, setProgram] = useState<Day[]>(initial.program);
  const [weeks, setWeeks] = useState<Week[]>(initial.weeks);
  const [phases, setPhases] = useState<Phase[]>(initial.phases);
  const [viewedWeekId, setViewedWeekId] = useState<Id | null>(initial.viewing ?? null);
  const [history, setHistory] = useState<HistoryState>(initial.history ?? EMPTY_HISTORY);
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null);

  // Hydrated once from the injected plan; applyPlanData never touches them.
  const [athlete] = useState<AthleteIdentity | null>(identity.athlete);
  const [group] = useState<GroupIdentity | null>(identity.group);

  const applyPlanData = useCallback((data: PlanEnvelope) => {
    setProgram(data.program);
    setWeeks(data.weeks);
    setPhases(data.phases);
    setViewedWeekId(data.viewing ?? null);
    setHistory(data.history ?? EMPTY_HISTORY);
    // Any grid swap invalidates an armed delete (it anchors a day by index).
    setPendingDelete(null);
  }, []);

  const adoptHistory = useCallback((data: HistoryCarrier) => {
    if (data && data.history) setHistory(data.history);
  }, []);

  const patchExercise = useCallback((exId: Id, patch: Partial<Exercise>) => {
    setProgram((prev) =>
      prev.map((day) => ({
        ...day,
        exercises: day.exercises.map((ex) => (ex.id === exId ? { ...ex, ...patch } : ex)),
      })),
    );
  }, []);

  const updateExerciseField = useCallback(
    (dayIndex: number, exIndex: number, field: keyof Exercise, value: string) => {
      setProgram((prev) =>
        prev.map((day, di) => {
          if (di !== dayIndex) return day;
          return {
            ...day,
            exercises: day.exercises.map((ex, xi) =>
              xi === exIndex ? { ...ex, [field]: value } : ex,
            ),
          };
        }),
      );
    },
    [],
  );

  const addExercise = useCallback(
    async (dayIndex: number) => {
      const day = program[dayIndex];
      if (!day) return;
      try {
        const data = await apiPost<{ prescription: Exercise; history?: HistoryState }>(
          `/meso/api/plan/${planId}/session/${day.id}/exercise/`,
          null,
          csrf,
        );
        setProgram((prev) =>
          prev.map((d, di) =>
            di === dayIndex ? { ...d, exercises: [...d.exercises, data.prescription] } : d,
          ),
        );
        adoptHistory(data);
      } catch (err) {
        console.error("Add exercise failed", err);
      }
    },
    [program, planId, csrf, adoptHistory],
  );

  const addDay = useCallback(async () => {
    try {
      const data = await apiPost<{ session: Day; history?: HistoryState }>(
        `/meso/api/plan/${planId}/session/`,
        { week_id: viewedWeekId },
        csrf,
      );
      setProgram((prev) => [...prev, data.session]);
      adoptHistory(data);
    } catch (err) {
      console.error("Add day failed", err);
    }
  }, [planId, viewedWeekId, csrf, adoptHistory]);

  const switchWeek = useCallback(
    async (weekId: Id) => {
      if (weekId == null || weekId === viewedWeekId) return;
      try {
        const res = await fetch(`/meso/api/plan/${planId}/week/${weekId}/`);
        if (!res.ok) throw new Error("Request failed: " + res.status);
        applyPlanData((await res.json()) as PlanEnvelope);
      } catch (err) {
        console.error("Switch week failed", err);
      }
    },
    [planId, viewedWeekId, applyPlanData],
  );

  const addWeek = useCallback(async () => {
    try {
      const data = await apiPost<PlanEnvelope>(`/meso/api/plan/${planId}/week/`, null, csrf);
      applyPlanData(data);
    } catch (err) {
      console.error("Add week failed", err);
    }
  }, [planId, csrf, applyPlanData]);

  const setCurrentWeek = useCallback(
    async (weekId: Id) => {
      if (weekId == null) return;
      try {
        const data = await apiPost<PlanEnvelope>(
          `/meso/api/plan/${planId}/week/${weekId}/current/`,
          null,
          csrf,
        );
        applyPlanData(data);
      } catch (err) {
        console.error("Set current week failed", err);
      }
    },
    [planId, csrf, applyPlanData],
  );

  const currentWeek = useMemo(
    () => weeks.find((w) => w.current) ?? weeks[0] ?? null,
    [weeks],
  );
  const viewedWeek = useMemo(
    () => weeks.find((w) => w.id === viewedWeekId) ?? currentWeek,
    [weeks, viewedWeekId, currentWeek],
  );
  const weekIsViewed = useCallback(
    (w: Week) => !!w && w.id === viewedWeekId,
    [viewedWeekId],
  );
  const viewedIsCurrent = !!(viewedWeek && viewedWeek.current);
  const currentPhase = useMemo(
    () => phases.find((p) => p.state === "current") ?? phases[0] ?? null,
    [phases],
  );

  const cycleLabel = useMemo(() => {
    const phase = currentPhase ? currentPhase.name : "";
    const wk = viewedWeek
      ? viewedWeek.label + (weeks.length ? " / " + weeks.length : "")
      : "";
    return [phase, wk].filter(Boolean).join(" · ");
  }, [currentPhase, viewedWeek, weeks.length]);

  const weekHeading = useMemo(() => {
    if (!viewedWeek) return "This week";
    return viewedWeek.phase ? viewedWeek.label + " — " + viewedWeek.phase : viewedWeek.label;
  }, [viewedWeek]);

  const blockHeading = useMemo(() => {
    if (!currentPhase) return "Mesocycle";
    return currentPhase.name + " — " + currentPhase.weeks + " mesocycle";
  }, [currentPhase]);

  const deliverHref = useMemo(
    () => buildDeliverHref(planId, viewedWeekId),
    [planId, viewedWeekId],
  );

  return {
    program,
    weeks,
    phases,
    viewedWeekId,
    history,
    athlete,
    group,
    pendingDelete,
    setPendingDelete,

    applyPlanData,
    adoptHistory,
    patchExercise,
    updateExerciseField,
    addExercise,
    addDay,
    switchWeek,
    addWeek,
    setCurrentWeek,

    currentWeek,
    viewedWeek,
    weekIsViewed,
    viewedIsCurrent,
    currentPhase,
    cycleLabel,
    weekHeading,
    blockHeading,
    deliverHref,
  };
}
