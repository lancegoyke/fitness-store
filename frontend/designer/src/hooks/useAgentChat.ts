// useAgentChat — the agent composer/thread (CONTRACT.md "useAgentChat"),
// ported from createMeso()'s pushCoach/pushAgent/onInputKey/onSend/onChip/
// send/sendInstruction/resumeDrafting/hydrateThread (app/store_project/
// static/js/meso.js). pollBatch/agentErrorText/batchMessage live in
// lib/agent.ts — this hook is the stateful wrapper around them.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { agentErrorText, pollBatch } from "../lib/agent";
import type { AgentMessage, ChatChange } from "../lib/agent";
import type { Id } from "./useGrid";

export interface ChatMessage {
  id: number;
  role: "agent" | "coach";
  text: string;
  changes?: ChatChange[];
  reviewUrl?: string | null;
  error?: boolean;
}

export interface UseAgentChatOptions {
  planId: Id;
  csrf: string;
  initialMessages: ChatMessage[];
  initialResumeUrl: string | null;
}

// Each chip's label is sent verbatim as the agent instruction.
const CHIPS: { label: string }[] = [
  { label: "Lower Day 2 volume" },
  { label: "Swap a knee-sensitive lift" },
  { label: "Progress from last block" },
  { label: "Add a deload week" },
];

export function useAgentChat(options: UseAgentChatOptions) {
  const { planId, csrf, initialMessages, initialResumeUrl } = options;

  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [inputText, setInputText] = useState("");
  const [agentTyping, setAgentTyping] = useState(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const idSeq = useRef(1_000_000);

  const nextId = useCallback(() => {
    idSeq.current += 1;
    return idSeq.current;
  }, []);

  useEffect(() => {
    const t = threadRef.current;
    if (t) t.scrollTop = t.scrollHeight;
  }, [messages, agentTyping]);

  const pushCoach = useCallback(
    (text: string) => {
      setMessages((prev) => [...prev, { id: nextId(), role: "coach", text }]);
    },
    [nextId],
  );

  const pushAgent = useCallback(
    (msg: AgentMessage) => {
      setMessages((prev) => [...prev, { id: nextId(), role: "agent", ...msg }]);
    },
    [nextId],
  );

  const sendInstruction = useCallback(
    async (instruction: string) => {
      setAgentTyping(true);
      try {
        const res = await fetch(`/meso/api/plan/${planId}/agent/`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrf,
          },
          body: JSON.stringify({ instruction }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          pushAgent({ text: agentErrorText(res.status, data), error: true });
          return;
        }
        await pollBatch(data.status_url, { onMessage: pushAgent });
      } catch (err) {
        console.error("Agent request failed", err);
        pushAgent({
          text: "Something went wrong reaching the agent. Please try again.",
          error: true,
        });
      } finally {
        setAgentTyping(false);
      }
    },
    [planId, csrf, pushAgent],
  );

  const send = useCallback(
    (instruction: string) => {
      pushCoach(instruction);
      void sendInstruction(instruction);
    },
    [pushCoach, sendInstruction],
  );

  const onSend = useCallback(() => {
    const t = inputText.trim();
    if (!t || agentTyping) return;
    setInputText("");
    send(t);
  }, [inputText, agentTyping, send]);

  const onChip = useCallback(
    (label: string) => {
      if (agentTyping) return;
      send(label);
    },
    [agentTyping, send],
  );

  const onInputKey = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        onSend();
      }
    },
    [onSend],
  );

  // Resume-from-thread: a hydrated batch was still drafting when the page
  // rendered. DesignerRoot computes initialResumeUrl once at hydration time;
  // resume it here the same way resumeDrafting did.
  useEffect(() => {
    if (!initialResumeUrl) return;
    let cancelled = false;
    (async () => {
      setAgentTyping(true);
      try {
        await pollBatch(initialResumeUrl, {
          onMessage: (msg) => {
            if (!cancelled) pushAgent(msg);
          },
        });
      } finally {
        if (!cancelled) setAgentTyping(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one-time mount resume
  }, []);

  return {
    messages,
    inputText,
    setInputText,
    agentTyping,
    chips: CHIPS,
    threadRef: threadRef as RefObject<HTMLDivElement>,
    onInputKey,
    onSend,
    onChip,
  };
}
