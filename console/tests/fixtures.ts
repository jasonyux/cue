import type { BackendCapabilities, ScenarioManifest } from "@cue/contracts";

export const scenarioFixture: ScenarioManifest = {
  id: "demo-scenario", labId: "kanban", category: "productivity",
  title: "Demo App", description: "A generic test scenario.", defaultPrompt: "Complete the demo task.",
  workspaceTemplatePath: "/tmp/demo-template", tags: ["test"],
};

export const pythonCapabilities: BackendCapabilities = {
  instanceId: "python-test-instance",
  backendId: "python", codeTool: "exec_py", browserModes: ["headful"],
  defaults: { browserMode: "headful", model: "python-model", maxResponseTurns: 18 },
};
export const pythonBackendProps = { capabilities: pythonCapabilities, expectedBackend: "python" as const };
