"use client";

import { agentModelsSchema, type AgentModel, type LiveSettings } from "@cue/contracts";
import { useEffect, useState } from "react";

import { callRunner } from "../live/shared";

// The chat's agent and its model, as the themes offer them: the conversation's own agent once it has one, else the one
// picked (or claude, cmux, the first in ``settings``: the session's, or the defaults before one starts); the models Claude
// Code or Codex list for the signed-in user, fetched once per agent, with the setting or the agent's own default chosen
// until the person picks. ``model`` is undefined while the agent lists none: the message then carries no model.
export function useAgentChoice(settings: LiveSettings | null, chatAgent: string | null | undefined, runnerBaseUrl: string, backend: string) {
  const agents: string[] = settings?.actorAgents ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const agent = chatAgent ?? (chosen && agents.includes(chosen) ? chosen : agents.includes("claude") ? "claude" : agents.includes("cmux") ? "cmux" : agents[0] ?? "claude");
  const [lists, setLists] = useState<Record<string, AgentModel[]>>({});
  const [picked, setPicked] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!settings || (agent !== "claude" && agent !== "codex") || lists[agent]) return;
    callRunner(runnerBaseUrl, backend, `/api/live/agents/${agent}/models`, agentModelsSchema)
      .then((answer) => setLists((current) => ({ ...current, [agent]: answer.models })))
      .catch(() => setLists((current) => ({ ...current, [agent]: [] })));
  }, [agent, settings, lists, runnerBaseUrl, backend]);
  const models = lists[agent] ?? [];
  const sessionModel = (agent === "claude" ? settings?.actorClaudeModel : agent === "codex" ? settings?.actorCodexModel : null) ?? null;
  const model = models.length ? picked[agent] ?? sessionModel ?? models.find((item) => item.default)?.id ?? "" : undefined;
  return { agents, agent, setAgent: setChosen, models, model, sessionModel, setModel: (id: string, forAgent = agent) => setPicked((current) => ({ ...current, [forAgent]: id })) };
}
