// DesignerRoot (CONTRACT.md "DesignerRoot") — the Phase 2 PR B island root.
// Hydrates once from the #meso-plan-data / #meso-chat-thread / #meso-csrf /
// #meso-designer-flags json_script elements the same way designer.html's
// init()/hydrateThread() did, composes every hook, and renders the tree.
import { useState } from "react";
import "./designer.css";

import { TopBar } from "./components/TopBar";
import type { DesignerMode } from "./components/TopBar";
import { LeftRail } from "./components/LeftRail";
import { ChatPanel } from "./components/ChatPanel";
import type { DesignerFlags } from "./components/ChatPanel";
import { WeekGrid } from "./components/WeekGrid";
import { BlockView } from "./components/BlockView";
import type { PeriodStyle } from "./components/BlockView";
import { AthletePreview } from "./components/AthletePreview";
import { OverrideModal } from "./components/OverrideModal";

import { usePlanData } from "./hooks/usePlanData";
import type { Id } from "./hooks/usePlanData";
import { useAutosave } from "./hooks/useAutosave";
import { useDeletes } from "./hooks/useDeletes";
import { useUndoRedo } from "./hooks/useUndoRedo";
import { useOverrideEditor } from "./hooks/useOverrideEditor";
import { useOneRmEditor } from "./hooks/useOneRmEditor";
import { useAgentChat } from "./hooks/useAgentChat";
import type { ChatMessage } from "./hooks/useAgentChat";
import { useCoachmarks } from "./hooks/useCoachmarks";

import type { AthleteIdentity, Day, GroupIdentity, HistoryState, Phase, PlanSummary, Week } from "./lib/api";

type ViewMode = "week" | "block" | "athlete";

interface HydratedPlanPayload {
  plan: PlanSummary;
  group: GroupIdentity | null;
  athlete: AthleteIdentity | null;
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: Id | null;
  history?: HistoryState;
}

interface Hydrated {
  planId: Id;
  unit: string;
  csrf: string;
  group: GroupIdentity | null;
  athlete: AthleteIdentity | null;
  program: Day[];
  weeks: Week[];
  phases: Phase[];
  viewing: Id | null;
  history?: HistoryState;
  initialMode: DesignerMode;
  initialMessages: ChatMessage[];
  initialResumeUrl: string | null;
  flags: DesignerFlags;
}

// The thread starts with a single orienting greeting — a group-aware opening
// line when the plan has a group, ported from init()'s override. Real agent
// turns append live as the coach sends instructions.
const DEFAULT_GREETING: ChatMessage = {
  id: 1,
  role: "agent",
  text: "Tell me how you'd like to adjust this plan — try “lighten Friday” or “add a deload week.”",
};
const GROUP_GREETING: ChatMessage = {
  id: 1,
  role: "agent",
  text:
    "This is the group's shared program — every member trains off it. Ask me to change it for the whole group, or to " +
    "adjust one athlete (a swap, a load %, or a volume tweak just for them). I propose changes for you to review and " +
    "honor every member's contraindications.",
};

const DEFAULT_FLAGS: DesignerFlags = {
  is_sandbox: false,
  can_use_agent: false,
  agent_allowance: { metered: false, allowance: 0, remaining: null, can_use: false, tier: "unlimited" },
  signup_url: "/meso/sandbox/signup/",
  price_summary: "",
};

/** Reads the four hydration json_script elements once, mirroring meso.js's
 * init()/hydrateThread(). Absent/unparseable plan data -> null (the
 * no-op-without-a-plan guard ported from init()'s early return). */
function readHydration(): Hydrated | null {
  const dataEl = document.getElementById("meso-plan-data");
  if (!dataEl) return null;

  let data: HydratedPlanPayload;
  try {
    data = JSON.parse(dataEl.textContent || "") as HydratedPlanPayload;
  } catch (err) {
    console.error("Could not parse plan data", err);
    return null;
  }

  const group = data.group ?? null;
  const athlete = data.athlete ?? null;

  let initialMessages: ChatMessage[] = group ? [GROUP_GREETING] : [DEFAULT_GREETING];
  let initialResumeUrl: string | null = null;
  const threadEl = document.getElementById("meso-chat-thread");
  if (threadEl) {
    try {
      const thread = JSON.parse(threadEl.textContent || "") as (ChatMessage & { pollUrl?: string })[];
      if (Array.isArray(thread) && thread.length) {
        const messages = [...thread];
        const last = messages[messages.length - 1];
        if (last && last.pollUrl) {
          messages.pop();
          initialResumeUrl = last.pollUrl;
        }
        initialMessages = messages;
      }
    } catch (err) {
      console.error("Could not parse chat thread", err);
    }
  }

  const csrfEl = document.getElementById("meso-csrf") as HTMLElement | null;
  const csrf = csrfEl?.dataset.token ?? "";

  let flags = DEFAULT_FLAGS;
  const flagsEl = document.getElementById("meso-designer-flags");
  if (flagsEl) {
    try {
      flags = JSON.parse(flagsEl.textContent || "") as DesignerFlags;
    } catch (err) {
      console.error("Could not parse designer flags", err);
    }
  }

  return {
    planId: data.plan.id,
    unit: data.plan.unit || "kg",
    csrf,
    group,
    athlete,
    program: data.program,
    weeks: data.weeks,
    phases: data.phases,
    viewing: data.viewing ?? null,
    history: data.history,
    initialMode: group ? "group" : "individual",
    initialMessages,
    initialResumeUrl,
    flags,
  };
}

export function DesignerRoot() {
  const [hydrated] = useState<Hydrated | null>(() => readHydration());
  const [mode, setMode] = useState<DesignerMode>(hydrated?.initialMode ?? "individual");
  const [view, setView] = useState<ViewMode>("week");
  const [periodStyle, setPeriodStyle] = useState<PeriodStyle>("timeline");
  const [checks, setChecks] = useState<Record<string, boolean>>({});

  const planId: Id = hydrated?.planId ?? "";
  const csrf = hydrated?.csrf ?? "";
  const unit = hydrated?.unit ?? "kg";
  const isIndividual = mode === "individual";
  const isGroup = mode === "group";

  // usePlanData is the sole owner of program/weeks/phases (CONTRACT.md rule 1).
  const planData = usePlanData(
    planId,
    csrf,
    {
      program: hydrated?.program ?? [],
      weeks: hydrated?.weeks ?? [],
      phases: hydrated?.phases ?? [],
      viewing: hydrated?.viewing ?? null,
      history: hydrated?.history,
    },
    { athlete: hydrated?.athlete ?? null, group: hydrated?.group ?? null },
  );

  const autosave = useAutosave({
    planId,
    csrf,
    patchExercise: planData.patchExercise,
    adoptHistory: planData.adoptHistory,
  });
  const deletes = useDeletes({
    planId,
    csrf,
    program: planData.program,
    weeks: planData.weeks,
    pendingDelete: planData.pendingDelete,
    setPendingDelete: planData.setPendingDelete,
    applyPlanData: planData.applyPlanData,
  });
  const undoRedo = useUndoRedo({
    planId,
    csrf,
    viewedWeekId: planData.viewedWeekId,
    history: planData.history,
    applyPlanData: planData.applyPlanData,
  });
  const overrideEditor = useOverrideEditor({
    planId,
    csrf,
    group: planData.group,
    adoptHistory: planData.adoptHistory,
    patchExercise: planData.patchExercise,
  });
  const oneRmEditor = useOneRmEditor({
    planId,
    csrf,
    isGroup,
    adoptHistory: planData.adoptHistory,
    patchExercise: planData.patchExercise,
  });
  const agentChat = useAgentChat({
    planId,
    csrf,
    initialMessages: hydrated?.initialMessages ?? [DEFAULT_GREETING],
    initialResumeUrl: hydrated?.initialResumeUrl ?? null,
  });
  const coachmarks = useCoachmarks();

  if (!hydrated) return null;

  const flags = hydrated.flags;

  return (
    <div className="meso-designer-root">
      <TopBar
        mode={mode}
        onSetMode={setMode}
        isIndividual={isIndividual}
        isGroup={isGroup}
        athlete={planData.athlete}
        group={planData.group}
        cycleLabel={planData.cycleLabel}
        onPreviewAsAthlete={() => setView("athlete")}
        deliverHref={planData.deliverHref}
      />

      <div className="meso-designer-body">
        <LeftRail
          isIndividual={isIndividual}
          isGroup={isGroup}
          athlete={planData.athlete}
          group={planData.group}
          phases={planData.phases}
          onOpenBlockView={() => setView("block")}
        />

        <ChatPanel
          messages={agentChat.messages}
          agentTyping={agentChat.agentTyping}
          chips={agentChat.chips}
          inputText={agentChat.inputText}
          onInputChange={agentChat.setInputText}
          onInputKey={agentChat.onInputKey}
          onSend={agentChat.onSend}
          onChip={agentChat.onChip}
          threadRef={agentChat.threadRef}
          flags={flags}
        />

        <div className="meso-canvas">
          <div className="meso-canvas-header">
            <div className="meso-seg">
              <button type="button" className={`meso-seg-btn meso-seg-btn--v${view === "week" ? " is-on" : ""}`} onClick={() => setView("week")}>
                This week
              </button>
              <button type="button" className={`meso-seg-btn meso-seg-btn--v${view === "block" ? " is-on" : ""}`} onClick={() => setView("block")}>
                Periodization
              </button>
              <button
                type="button"
                className={`meso-seg-btn meso-seg-btn--v${view === "athlete" ? " is-on" : ""}`}
                onClick={() => setView("athlete")}
              >
                Athlete view
              </button>
            </div>
            <div className="meso-flex-spacer" />
            {view === "week" && (
              <div className="meso-flex meso-canvas-autosaved">
                <div className="meso-canvas-autosaved-dot" />
                Autosaved · last edit just now
              </div>
            )}
          </div>

          <div className="meso-canvas-body">
            {view === "week" && (
              <div className="meso-week-view">
                <div className="meso-week-view-head">
                  <div>
                    <h1 className="meso-week-view-title">{planData.weekHeading}</h1>
                    <p className="meso-week-view-sub">
                      {planData.program.length + (planData.program.length === 1 ? " session" : " sessions") + " · tap any cell to edit"}
                    </p>
                  </div>
                </div>

                <WeekGrid
                  program={planData.program}
                  isGroup={isGroup}
                  unit={unit}
                  pendingDelete={planData.pendingDelete}
                  deleting={deletes.deleting}
                  onRequestRemoveDay={deletes.requestRemoveDay}
                  onConfirmPendingDelete={deletes.confirmPendingDelete}
                  onCancelPendingDelete={deletes.cancelPendingDelete}
                  onAddDay={planData.addDay}
                  onAddExercise={planData.addExercise}
                  onFieldChange={planData.updateExerciseField}
                  onCommit={(di, xi) => {
                    const ex = planData.program[di]?.exercises[xi];
                    if (ex) autosave.persistRow(ex);
                  }}
                  onRemoveExercise={deletes.removeExercise}
                  onToggleLoadType={autosave.toggleLoadType}
                  onOpenOverride={overrideEditor.openOverride}
                  onOpenOneRm={oneRmEditor.openOneRm}
                  onOneRmChange={oneRmEditor.updateValue}
                  onOneRmSave={oneRmEditor.saveOneRm}
                  onOneRmCancel={oneRmEditor.closeOneRm}
                  oneRmOpenForRow={(ex) => !!oneRmEditor.oneRm && oneRmEditor.oneRm.ex.id === ex.id}
                  oneRmEditorState={oneRmEditor.oneRm}
                  coachmarkVisible={coachmarks.coachmarkVisible}
                  dismissCoachmark={coachmarks.dismissCoachmark}
                  weeks={planData.weeks}
                  viewedWeekId={planData.viewedWeekId}
                  viewedIsCurrent={planData.viewedIsCurrent}
                  history={planData.history}
                  undoing={undoRedo.undoing}
                  onSwitchWeek={planData.switchWeek}
                  onAddWeek={planData.addWeek}
                  onMakeCurrent={planData.setCurrentWeek}
                  onRequestRemoveWeek={deletes.requestRemoveWeek}
                  onUndo={undoRedo.undo}
                  onRedo={undoRedo.redo}
                />
              </div>
            )}

            {view === "block" && (
              <BlockView
                phases={planData.phases}
                weeks={planData.weeks}
                periodStyle={periodStyle}
                onSetPeriodStyle={setPeriodStyle}
                onSwitchWeek={planData.switchWeek}
              />
            )}

            {view === "athlete" && (
              <AthletePreview
                program={planData.program}
                unit={unit}
                checks={checks}
                onToggleCheck={(k) => setChecks((prev) => ({ ...prev, [k]: !prev[k] }))}
              />
            )}
          </div>
        </div>
      </div>

      <OverrideModal
        override={overrideEditor.override}
        overrideHasExisting={overrideEditor.overrideHasExisting}
        unit={unit}
        onSelectMember={overrideEditor.selectOverrideMember}
        onUpdateDraft={overrideEditor.updateDraft}
        onClose={overrideEditor.closeOverride}
        onSave={overrideEditor.saveOverride}
        onClear={overrideEditor.clearOverride}
      />
    </div>
  );
}
