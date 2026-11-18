"use client";

import { liveSessionStatusSchema, liveSettingsCheckSchema, type LiveSettings, type LiveSettingsCheck } from "@cue/contracts";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { callRunner } from "../live/shared";
import { FIELDS, GROUPS, LEVELS, SIMPLE, parsed, readOverrides, shown, writeOverrides, type Field, type Overrides } from "./overrides";
import { AGENT, HELP_PAGES, MODELS, Quickstart, SHARED, STEPS, VIA, markSeen } from "./Quickstart";
import { Banners, TopBar } from "./Shell";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };

const MODE_KEY = "v1.settingsMode"; // the view this browser last chose: simple (the default) or advanced
const fieldOf = (key: string): Field => FIELDS.find((item) => item.key === key)!;
type Row = { key: string; name: string; level: string }; // one permission rule on the page
type SimpleRow = (typeof SIMPLE)[number]["rows"][number] & { tip?: string }; // tip: a line under the controls, in the quickstart
const providerOf = (row: SimpleRow) => row.keys[0]!.replace(/ApiKey$|BaseUrl$/, ""); // of a Providers row

// The settings a session starts with, as .env (the providers' keys and base URLs) and settings.yaml (the rest) hold them:
// Save writes the changes there, and the backend reads them again for the next session; a running one keeps its own.
export function Settings({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const { refresh } = s;
  const defaults: LiveSettings | null = s.status?.defaults ?? null;
  const [overrides, setOverrides] = useState<Overrides>({});
  const [edits, setEdits] = useState<Partial<Record<keyof LiveSettings, Row[]>>>({}); // the rules fields' rows while edited here
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [checks, setChecks] = useState<LiveSettingsCheck | null>(null);
  const results = useRef<HTMLUListElement>(null); // the checks list, at the bottom: the warning mark scrolls to it
  const [checking, setChecking] = useState(false);
  const [group, setGroup] = useState("Session");
  const [advanced, setAdvanced] = useState(false);
  const router = useRouter();
  const first = useSearchParams().get("quickstart") !== null; // the landing page sent a first-time user here
  const [quick, setQuick] = useState<number | null>(first ? 0 : null); // the quickstart's step, while its window is up
  const [via, setVia] = useState<(typeof VIA)[number][0]>("page"); // on its keys card: where the keys go
  const [added, setAdded] = useState(""); // the variables last written to .env from it
  useEffect(() => {
    setOverrides(readOverrides()); // changes an earlier version kept in this browser: shown as changes, to save
    try { setAdvanced(window.localStorage.getItem(MODE_KEY) === "advanced"); } catch { /* storage blocked: the simple view */ }
  }, []);
  // The backend reads the files again whenever the page opens, so one edited by hand shows here.
  const write = useCallback(async (values: Overrides) => {
    await callRunner(runnerBaseUrl, expectedBackend, "/api/live/settings", liveSessionStatusSchema, { method: "POST", body: JSON.stringify(values) });
    await refresh();
  }, [runnerBaseUrl, expectedBackend, refresh]);
  useEffect(() => { void write({}).catch(() => { /* the backend is down: the banner says so */ }); }, [write]);
  const choose = (next: boolean) => {
    setAdvanced(next);
    try { window.localStorage.setItem(MODE_KEY, next ? "advanced" : "simple"); } catch { /* storage blocked: this visit only */ }
  };

  const value = (field: Field) => shown(field, field.key in overrides ? overrides[field.key] : field.kind === "secret" ? "" : defaults?.[field.key]);
  // A secret, never shown back whole: the held one abbreviated, "" when the files hold none.
  const keyHint = (field: Field) => field.key === "desktopRemoteToken" ? defaults?.desktopRemoteTokenHint ?? ""
    : defaults?.providerKeys?.[field.key.replace("ApiKey", "") as keyof NonNullable<LiveSettings["providerKeys"]>] ?? "";
  // A provider a model uses with nothing to reach it by: no key (vLLM, which mostly takes none: no address). Its row turns red.
  const current = (key: keyof LiveSettings) => (key in overrides ? overrides[key] : defaults?.[key]);
  const roles: (keyof LiveSettings)[] = ["summaryProvider", "beliefProvider", "proposerProvider", "preferenceProvider", ...(current("proposerDedup") ? ["embedProvider" as const] : [])];
  const missing = (key: keyof LiveSettings) => {
    const provider = key.replace(/ApiKey$|BaseUrl$/, "");
    return key === (provider === "vllm" ? "vllmBaseUrl" : `${provider}ApiKey`) && roles.some((role) => current(role) === provider)
      && !current(key) && !(key.endsWith("ApiKey") && keyHint(fieldOf(key)));
  };
  const chosen = (field: Field) => value(field).split(",").filter(Boolean); // a set's picks
  // A rules field (permissions) is apart from Save: each row is written by its own ✓, or removed by its ×, and a row not yet
  // saved lives on the page only. ``key`` is the saved rule the row came from, "" for a new one.
  const saved = (field: Field) => (defaults?.[field.key] ?? {}) as Record<string, string>;
  const rows = (field: Field): Row[] => edits[field.key] ?? Object.entries(saved(field)).map(([name, level]) => ({ key: name, name, level }));
  const editRows = (field: Field, update: (all: Row[]) => Row[]) => setEdits((all) => ({ ...all, [field.key]: update(all[field.key] ?? rows(field)) }));
  const editRow = (field: Field, index: number, change: Partial<Row>) => editRows(field, (all) => all.map((row, at) => at === index ? { ...row, ...change } : row));
  const writeRules = async (field: Field, mapping: Record<string, string>, update: (all: Row[]) => Row[]) => {
    try { await write({ [field.key]: mapping }); editRows(field, update); setSaveError(null); } catch (error) { setSaveError(error instanceof Error ? error.message : "Could not save the rule."); }
  };
  const saveRule = (field: Field, index: number, row: Row) => {
    const mapping = { ...saved(field) };
    delete mapping[row.key];
    return writeRules(field, { ...mapping, [row.name.trim()]: row.level }, (all) => all.map((item, at) => at === index ? { ...item, key: item.name.trim() } : item));
  };
  const removeRule = (field: Field, index: number, row: Row) => {
    const mapping = { ...saved(field) };
    delete mapping[row.key];
    return row.key in saved(field) ? writeRules(field, mapping, (all) => all.filter((_, at) => at !== index)) : editRows(field, (all) => all.filter((_, at) => at !== index));
  };
  const unsaved = (field: Field, row: Row) => row.name.trim() !== row.key || saved(field)[row.key] !== row.level;
  const control = (field: Field, label?: string) => (
    field.kind === "rules" ? <span className="rules">
      {rows(field).map((row, index) => <span key={index}>
        <input type="text" aria-label={`${field.label} rule ${index + 1}`} placeholder={field.placeholder} value={row.name} onChange={(event) => editRow(field, index, { name: event.target.value })} />
        <select aria-label={`${field.label} rule ${index + 1} level`} value={row.level} onChange={(event) => editRow(field, index, { level: event.target.value })}>{LEVELS.map(([option, text]) => <option key={option} value={option}>{text}</option>)}</select>
        {row.name.trim() && unsaved(field, row) ? <button type="button" className="chip" aria-label={`save ${field.label} rule ${index + 1}`} onClick={() => void saveRule(field, index, row)}>✓</button> : null}
        <button type="button" className="chip" aria-label={`remove ${field.label} rule ${index + 1}`} onClick={() => void removeRule(field, index, row)}>×</button>
      </span>)}
      <button type="button" className="chip" aria-label={`add ${field.label} rule`} onClick={() => editRows(field, (all) => [...all, { key: "", name: "", level: "block" }])}>+</button>
    </span>
      : field.kind === "set" ? <span className="set">{field.options?.map(([option, text]) => <label key={option}><input type="checkbox" checked={chosen(field).includes(option)} disabled={chosen(field).length === 1 && chosen(field)[0] === option} onChange={(event) => change(field, (event.target.checked ? [...chosen(field), option] : chosen(field).filter((item) => item !== option)).join(","))} /> {text}</label>)}</span>
      : field.kind === "bool" ? <input id={field.key} type="checkbox" aria-label={label} checked={value(field) === "true"} onChange={(event) => change(field, String(event.target.checked))} />
      : field.kind === "select" ? <select id={field.key} aria-label={label} value={value(field)} onChange={(event) => change(field, event.target.value)}>{field.options?.map(([option, text]) => <option key={option} value={option}>{text}</option>)}</select>
      : field.kind === "secret" ? <input id={field.key} type="password" autoComplete="off" aria-label={label} value={value(field)} placeholder={keyHint(field) || "not set"} onChange={(event) => change(field, event.target.value)} />
      : <input id={field.key} type={field.kind === "number" ? "number" : "text"} aria-label={label} className={field.kind === "number" ? "short" : ""} value={value(field)} onChange={(event) => change(field, event.target.value)} />
  );
  const change = (field: Field, text: string) => {
    const next = { ...overrides };
    const shared = quick !== null && !each ? SHARED.find(([key]) => key === field.key)?.[1] ?? [] : []; // the quickstart's Agents row sets the three agents
    for (const item of [field, ...shared.map(fieldOf)]) {
      const given = item.kind === "bool" ? text === "true" : parsed(item, text);
      if (item.kind === "secret" ? !text : shown(item, given) === shown(item, defaults?.[item.key])) delete next[item.key]; else next[item.key] = given;
    }
    setOverrides(next);
  };
  const save = async () => { // whether it was written
    setSaving(true);
    setSaveError(null);
    try {
      await write(overrides);
      setOverrides({});
      writeOverrides({});
      return true;
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not save the settings.");
      return false;
    } finally {
      setSaving(false);
    }
  };
  const discard = () => { setOverrides({}); writeOverrides({}); setSaveError(null); };
  const test = async () => {
    setChecking(true);
    try {
      await write({}); // the files as they are now: a key put in .env by hand counts
      setChecks(await callRunner(runnerBaseUrl, expectedBackend, "/api/live/settings/check", liveSettingsCheckSchema, { method: "POST", body: JSON.stringify(overrides) }, 45_000));
    } catch (error) {
      setChecks({ ok: false, checks: [{ label: "Runner", role: "runner", ok: false, error: error instanceof Error ? error.message : "Could not test the configuration." }] });
    } finally {
      setChecking(false);
    }
  };
  const changed = Object.keys(overrides).length;
  const checkLine = (check: LiveSettingsCheck["checks"][number]) => <li key={check.label} className={check.ok ? "ok" : "fail"}>{check.ok ? "✓" : "✗"} {check.label}{check.model ? ` · ${check.model}` : ""}{check.baseUrl ? ` @ ${check.baseUrl}` : ""}{check.ok ? ` · ${check.latencyMs ?? "?"} ms${check.detail ? ` · ${check.detail}` : ""}` : ` · ${check.error ?? "failed"}`}</li>;
  const checkList = checks ? <ul className="checks" ref={results}>{checks.checks.map(checkLine)}</ul> : null;
  // In the quickstart the results are short: one line when every check passed, the failed ones alone otherwise.
  const failed = checks?.checks.filter((check) => !check.ok) ?? [];
  const quickChecks = checks && !checking ? <ul className="checks">{failed.length ? failed.map(checkLine) : <li className="ok">✓ All good</li>}</ul> : null;
  // One row of the simple view: a setting, or a few side by side.
  const simpleRow = (row: SimpleRow) => (
    <div key={row.label} className={`row${row.keys.some((key) => key in overrides) ? " changed" : ""}${row.keys.some(missing) ? " missing" : ""}`}>
      <label htmlFor={row.keys[0]}>{row.label}{quick !== null && HELP_PAGES[providerOf(row)] ? <a className="quick-help" href={HELP_PAGES[providerOf(row)]} target="_blank" rel="noreferrer" aria-label={`About ${row.label}`}>?</a> : null}{row.hint ? <small>{row.hint}</small> : null}</label>
      <div className="inline">{row.keys.map((key) => <span key={key}>{fieldOf(key).short ? <small>{fieldOf(key).short}</small> : null}{control(fieldOf(key), `${row.label} ${fieldOf(key).label}`)}{fieldOf(key).unit ? <span className="unit">{fieldOf(key).unit}</span> : null}</span>)}</div>
      {row.tip ? <small className="tip">{row.tip}</small> : null}
    </div>
  );
  // The quickstart: three cards over the page, in its own words (the simple view keeps its own). The main agent and the
  // session's length, in hours; the background models and where they come from; then a key for every provider those
  // models use. The models are three rows (the agents share one) unless the saved settings hold a different one per
  // agent: then each has its row, as in the simple view.
  const each = SHARED.some(([key, others]) => others.some((other) => defaults?.[other] !== defaults?.[key]));
  const rowsOf = (title: string) => SIMPLE.find((section) => section.title === title)!.rows;
  const quickRows = quick === 0 ? [AGENT]
    : quick === 1 ? (each ? rowsOf("Models").map((row) => ({ ...row, keys: row.keys.filter((key) => key !== "proposerDedup") })) : MODELS) // without the repeat filter's switch: it stays on, as it comes
    : rowsOf("Providers").filter((row) => roles.some((role) => current(role) === providerOf(row)));
  // The keys card also takes keys kept off the page: its rows' variables, for a .env filled in by hand (their names can
  // be written there, blank where no key is held) or for the shell before launching.
  const keys = quick === STEPS.length - 1;
  const variables = (prefix: string) => quickRows.flatMap((row) => row.keys).map((key) => `${prefix}${key.replace(/ApiKey$/, "_API_KEY").replace(/BaseUrl$/, "_BASE_URL").toUpperCase()}=${key.endsWith("BaseUrl") ? value(fieldOf(key)) : ""}`);
  const names = variables("").join("\n");
  const addNames = async () => {
    const fields = quickRows.flatMap((row) => row.keys).filter((key) => key.endsWith("BaseUrl") || !keyHint(fieldOf(key)));
    try {
      await write(Object.fromEntries(fields.map((key) => [key, key.endsWith("BaseUrl") ? value(fieldOf(key)) : ""])));
      setAdded(names);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not write .env.");
    }
  };
  const length = fieldOf("maxSessionMinutes");
  const hours = (
    <div className={`row${length.key in overrides ? " changed" : ""}`}>
      <label htmlFor="sessionHours">Session length</label>
      <div className="inline"><input id="sessionHours" type="number" className="short" min={0} step={0.5} value={Math.round(Number(value(length)) / 0.6) / 100} onChange={(event) => change(length, String(Math.round(Number(event.target.value) * 60)))} /><span className="unit">hours</span></div>
    </div>
  );
  const quickBody = !keys || via === "page" ? <div className="simple">{quickRows.map(simpleRow)}{quick === 0 ? hours : null}</div>
    : <pre className="quick-code">{via === "file" ? names : [...variables("export "), "pnpm cue web"].join("\n")}</pre>;
  const closeQuick = () => { markSeen(); setQuick(null); if (first) router.push("/v1"); };
  const finishQuick = async () => { if (await save()) closeQuick(); };
  return (
    <div className="v1">
      <TopBar s={s} current="Settings" />
      <Banners s={s} />
      <div className="sub">
        <button type="button" className="quick-open" onClick={() => setQuick(0)}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5l1.6 4.9 4.9 1.6-4.9 1.6L8 14.5 6.4 9.6 1.5 8l4.9-1.6z" fill="currentColor" /></svg>Quickstart</button>
        <span className="tabs">
          <button type="button" className={`chip${advanced ? "" : " on"}`} onClick={() => choose(false)}>Simple</button>
          <button type="button" className={`chip${advanced ? " on" : ""}`} onClick={() => choose(true)}>Advanced</button>
        </span>
        {advanced ? <span className="tabs">{GROUPS.map((name) => <button key={name} type="button" className={`chip${group === name ? " on" : ""}`} onClick={() => setGroup(name)}>{name}</button>)}</span> : null}
        <span className="muted">{changed ? `${changed} unsaved` : "as saved"}</span>
        {saveError ? <span className="fail">{saveError}</span> : null}
        <span className="spacer" />
        {checks && !checking ? (checks.ok ? <span className="mark ok" title="every check passed">✓</span>
          : <button type="button" className="mark fail" title="a check failed: see the results" onClick={() => results.current?.scrollIntoView({ behavior: "smooth", block: "end" })}>⚠</button>) : null}
        <button type="button" className="btn sm" disabled={checking || !defaults} onClick={() => void test()}>{checking ? "Testing…" : "Test configuration"}</button>
        <button type="button" className="btn sm ghost" disabled={!changed} onClick={discard}>Discard changes</button>
        <button type="button" className="btn sm primary" disabled={!changed || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save"}</button>
      </div>
      <div className="page">
        {!defaults ? <p className="muted">Loading the defaults…</p> : quick !== null ? null : (
          <div className="form">
            {!advanced ? <p className="muted">The models and behavior of Cue: keys go to <code>.env</code>, everything else to <code>settings.yaml</code>. Switch to <b>Advanced</b> for every setting.</p> : null}
            {!advanced ? SIMPLE.map((section) => (
              <div key={section.title} className="simple">
                <h3>{section.title}</h3>
                {section.rows.map(simpleRow)}
              </div>
            )) : FIELDS.filter((field) => field.group === group).map((field) => (
              <div key={field.key} className={`row${field.kind === "rules" ? " stack" : ""}${field.key in overrides ? " changed" : ""}${missing(field.key) ? " missing" : ""}`}>
                <label htmlFor={field.key}>{field.label}{field.hint ? <small>{field.hint}</small> : null}</label>
                <div>
                  {control(field)}
                  {field.unit ? <span className="unit">{field.unit}</span> : null}
                  {field.key in overrides && field.kind !== "secret" ? <span className="unit">· default {shown(field, defaults[field.key]) || "blank"}</span> : null}
                </div>
              </div>
            ))}
            {checkList}
          </div>
        )}
      </div>
      {quick !== null && defaults ? (
        <Quickstart
          step={quick}
          onStep={setQuick}
          onClose={closeQuick}
          onFinish={() => void finishQuick()}
          busy={saving}
          error={saveError}
          extra={<button type="button" className="btn" disabled={checking} onClick={() => void test()}>{checking ? "Testing…" : "Test configuration"}</button>}
        >
          {keys ? <div className="quick-via">{VIA.map(([name, text]) => <button key={name} type="button" className={`chip${via === name ? " on" : ""}`} aria-pressed={via === name} onClick={() => setVia(name)}>{text}</button>)}</div> : null}
          {quickBody}
          {keys && via === "page" ? <p className="quick-note">saved to .env in Cue&apos;s folder</p> : null}
          {keys && via === "file" ? <button type="button" className="btn sm" disabled={added === names} onClick={() => void addNames()}>{added === names ? "Added ✓" : "Add to .env"}</button> : null}
          {keys ? quickChecks : null}
        </Quickstart>
      ) : null}
    </div>
  );
}
