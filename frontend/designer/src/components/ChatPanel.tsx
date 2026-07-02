// ChatPanel (CONTRACT.md "ChatPanel") — ported 1:1 from designer.html's agent
// column (lines ~203-313): thread (agent/coach bubbles, inline changes +
// review link, typing indicator) plus the three flags-gated footers that
// replace the template's server {% if is_sandbox %}/{% elif can_use_agent %}/
// {% else %} composer blocks with a client-side branch on hydrated data.
import type { KeyboardEvent, RefObject } from "react";
import type { ChatMessage } from "../hooks/useAgentChat";

export interface DesignerFlags {
  is_sandbox: boolean;
  can_use_agent: boolean;
  agent_allowance: {
    metered: boolean;
    allowance: number;
    remaining: number | null;
    can_use: boolean;
    tier: "unlimited" | "paid" | "free";
  };
  signup_url: string;
  price_summary: string;
}

export interface ChatPanelProps {
  messages: ChatMessage[];
  agentTyping: boolean;
  chips: { label: string }[];
  inputText: string;
  onInputChange(value: string): void;
  onInputKey(e: KeyboardEvent<HTMLInputElement>): void;
  onSend(): void;
  onChip(label: string): void;
  threadRef: RefObject<HTMLDivElement | null>;
  flags: DesignerFlags;
}

export function ChatPanel(props: ChatPanelProps) {
  const { messages, agentTyping, chips, inputText, onInputChange, onInputKey, onSend, onChip, threadRef, flags } = props;
  const allowance = flags.agent_allowance;

  return (
    <div className="meso-chat-panel">
      <div className="meso-chat-header">
        <span className="meso-chat-mark">
          <span className="meso-chat-mark-glyph" />
        </span>
        <div className="meso-chat-title">Agent</div>
        {agentTyping ? (
          <div className="meso-flex meso-status meso-status--typing">
            <span className="meso-status-dot" />
            drafting…
          </div>
        ) : (
          <div className="meso-flex meso-status meso-status--ready">
            <span className="meso-status-dot" />
            ready
          </div>
        )}
        <div className="meso-flex-spacer" />
      </div>

      <div className="meso-chat-note">
        <span className="meso-chat-note-title">Propose → review → apply</span>
        <span>You review every change before it touches the program. Nothing applies until you approve.</span>
      </div>

      <div ref={threadRef} className="meso-chat-thread">
        {messages.map((m) => (
          <div key={m.id}>
            {m.role === "agent" && (
              <div className="meso-msg-agent-wrap">
                <div className={`meso-msg-agent${m.error ? " meso-msg-agent--error" : ""}`}>{m.text}</div>
                {!!(m.changes && m.changes.length) && (
                  <div className="meso-msg-changes">
                    {m.changes!.map((ch) => (
                      <div key={ch.id} className="meso-change-chip">
                        <div className="meso-change-check">✓</div>
                        <div className="meso-change-body">
                          <div className="meso-change-title">{ch.title}</div>
                          {ch.member && <div className="meso-change-member">{ch.member}</div>}
                          <div className="meso-change-diff">
                            {ch.before && <span className="meso-change-before">{ch.before}</span>}
                            {ch.before && ch.after && <span> → </span>}
                            <span>{ch.after}</span>
                          </div>
                        </div>
                      </div>
                    ))}
                    <a data-testid="agent-review-link" href={m.reviewUrl ?? undefined} className="meso-review-link">
                      Review {m.changes!.length} {m.changes!.length === 1 ? "change" : "changes"} →
                    </a>
                  </div>
                )}
              </div>
            )}
            {m.role === "coach" && <div className="meso-msg-coach">{m.text}</div>}
          </div>
        ))}
        {agentTyping && (
          <div className="meso-typing">
            <span className="meso-typing-dot" />
            <span className="meso-typing-dot" />
            <span className="meso-typing-dot" />
          </div>
        )}
      </div>

      {flags.is_sandbox ? (
        <div className="meso-chat-footer">
          <p className="meso-footer-copy">Let AI draft the whole program for you — free to start.</p>
          <a data-testid="agent-sandbox-cta" href={flags.signup_url} data-hover="brighten" className="meso-btn-deliver meso-btn-deliver--inline">
            Create a free account
          </a>
        </div>
      ) : flags.can_use_agent ? (
        <div className="meso-chat-footer">
          <div className="meso-chip-row">
            {chips.map((c, i) => (
              <button
                key={i}
                type="button"
                data-testid={`agent-chip-${i}`}
                data-hover="chip"
                className="meso-chip"
                disabled={agentTyping}
                onClick={() => onChip(c.label)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <div className="meso-composer">
            <input
              data-testid="agent-composer-input"
              className="meso-composer-input"
              value={inputText}
              placeholder="Ask the agent to adjust the program…"
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={onInputKey}
            />
            <button
              type="button"
              data-testid="agent-composer-send"
              data-hover="brighten"
              className="meso-composer-send"
              disabled={agentTyping}
              onClick={onSend}
            >
              ↑
            </button>
          </div>
          {allowance.metered && (
            <p data-testid="agent-allowance-note" className="meso-allowance-note">
              {allowance.remaining} of {allowance.allowance} {allowance.tier === "free" ? "free " : ""}
              agent run{allowance.allowance === 1 ? "" : "s"} left this month
              {allowance.tier === "free" && (
                <>
                  {" · "}
                  <a href="/meso/" className="meso-inline-link">
                    subscribe for more
                  </a>
                </>
              )}
            </p>
          )}
        </div>
      ) : (
        <div className="meso-chat-footer">
          {allowance.tier === "free" ? (
            <>
              <p className="meso-footer-copy">
                You&rsquo;ve used all {allowance.allowance} free agent run{allowance.allowance === 1 ? "" : "s"} this month. Start
                your free trial or subscribe ({flags.price_summary}) for the full AI agent.
              </p>
              <a data-testid="agent-upgrade-cta" href="/meso/" data-hover="brighten" className="meso-btn-deliver meso-btn-deliver--inline">
                Upgrade to use the agent
              </a>
            </>
          ) : (
            <p className="meso-footer-copy">
              You&rsquo;ve used all {allowance.allowance} agent runs this month. Your allowance resets on the 1st.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
