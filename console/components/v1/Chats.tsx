"use client";

import type { LiveChatList, LiveConversation } from "@cue/contracts";
import { Fragment, type ReactNode, useState } from "react";

import { clockOf } from "../live/shared";
import { useMore } from "./more";

const AGENT: Record<string, string> = { claude: "Claude Code", codex: "Codex", cmux: "cmux + Claude Code", desktop: "Desktop" };
// The agents' marks (the logo paths of simple-icons, CC0), as Orca marks the driver of a workspace.
const CLAUDE = <svg className="claude" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" /></svg>;
const GLYPH: Record<string, ReactNode> = {
  claude: CLAUDE,
  cmux: CLAUDE,
  codex: <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" /></svg>,
  desktop: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8" /></svg>,
};
const FOLDER = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></svg>;
const GROUPS = ["Current", "Named", "Earlier"]; // the running session (paused or not, however many days), those given a name, the rest by date
const midnight = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
const dayOf = (day: Date) => {
  const gap = Math.round((midnight(new Date()) - midnight(day)) / 86_400_000);
  return gap === 0 ? "Today" : gap === 1 ? "Yesterday" : day.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};
const startOf = (id: string) => /^\d{8}-\d{6}$/.test(id) ? new Date(+id.slice(0, 4), +id.slice(4, 6) - 1, +id.slice(6, 8), +id.slice(9, 11), +id.slice(11, 13)) : null; // a session id is its local start time
export const folderName = (id: string, name: string | null | undefined) => name || (startOf(id) ? `${dayOf(startOf(id)!)} ${id.slice(9, 11)}:${id.slice(11, 13)}` : id);

type Folder = { id: string; items: LiveConversation[] };
type Props = {
  list: LiveChatList | null;
  current: string | null; // the conversation the next message goes to
  shown: string | null; // an earlier one open read-only
  session: string | null; // the session running, whose folder is marked
  working: string | null; // the conversation a turn is running in
  busy: boolean;
  onOpen: (item: LiveConversation) => void;
  onNew?: (() => void) | undefined; // absent with no session to start one in
  onMark: (id: string, mark: { title?: string; pinned?: boolean }) => void;
  onRenameSession: (id: string, name: string) => void;
};

// The sidebar's Conversations panel, a tree like Orca's workspaces: the pinned conversations on top, then a folder per
// session (its name, else when it started; the running one dotted green), the running one first, then the named ones,
// then the rest by date, open when it is the running one, the first, or holds the conversation in use; under it each conversation on one line, latest first: a
// status dot, its title, its agent's mark and when it last moved; those from sessions before conversations were kept
// muted (view-only). Each row's ⋯, shown on hover as in the harness, pins or renames it; a folder's renames the session.
export function Chats({ list, current, shown, session, working, busy, onOpen, onNew, onMark, onRenameSession }: Props) {
  const [flipped, setFlipped] = useState<Record<string, boolean>>({}); // folders opened or shut by hand, against the default
  const [editing, setEditing] = useState<string | null>(null); // "chat:<id>" or "session:<id>": whose name is being typed
  const { more, show } = useMore();
  const items = list?.conversations ?? [];
  const folders: Folder[] = [];
  for (const item of items.filter((item) => !item.pinned)) {
    const id = item.sessions?.[0] ?? "";
    (folders.find((folder) => folder.id === id) ?? folders[folders.push({ id, items: [] }) - 1]!).items.push(item);
  }
  const group = (folder: Folder) => folder.id === session ? 0 : list?.sessions?.[folder.id] ? 1 : 2;
  const groups = GROUPS.map((label, rank) => ({ label, folders: folders.filter((folder) => group(folder) === rank) })).filter((item) => item.folders.length);
  const first = folders.find((folder) => folder.id === session) ?? groups[0]?.folders[0]; // open by default: the running session's folder, else the first
  const rename = (value: string, placeholder: string, label: string, save: (text: string) => void) => ( // typed in the row's place: Enter keeps it, Escape or leaving drops it
    <input className="rename" autoFocus defaultValue={value} placeholder={placeholder} aria-label={label}
      onKeyDown={(event) => { if (event.key === "Enter") { save(event.currentTarget.value); setEditing(null); } else if (event.key === "Escape") setEditing(null); }}
      onBlur={() => setEditing(null)} />
  );
  const row = (item: LiveConversation) => (
    <div key={item.id} className={`thread${item.id === current ? " on" : ""}${item.id === shown ? " shown" : ""}${item.viewOnly ? " ro" : ""}`}>
      {editing === `chat:${item.id}` ? rename(item.title ?? item.firstMessage, "", "Rename the conversation", (text) => onMark(item.id, { title: text })) : (
        <>
          <button type="button" className="pick" title={`${AGENT[item.agent ?? ""] ?? item.agent ?? "?"} · ${item.firstMessage}`} disabled={busy} onClick={() => onOpen(item)} onContextMenu={(event) => { event.preventDefault(); show(`chat:${item.id}`); }}>
            <i className={`st${item.id === working ? " going" : item.id === current ? " on" : ""}`} /><span className="what">{item.title || item.firstMessage || "(no message)"}</span>{GLYPH[item.agent ?? ""]}<time>{clockOf(item.lastAt ?? item.startedAt).slice(0, 5)}</time>
          </button>
          {more(`chat:${item.id}`, [[item.pinned ? "Unpin" : "Pin", () => onMark(item.id, { pinned: !item.pinned })], ["Rename", () => setEditing(`chat:${item.id}`)]])}
        </>
      )}
    </div>
  );
  const folderRow = (folder: Folder, open: boolean) => (
    <div className={`folder${open ? " open" : ""}`}>
      {editing === `session:${folder.id}` ? rename(list?.sessions?.[folder.id] ?? "", folderName(folder.id, null), "Rename the session", (text) => onRenameSession(folder.id, text)) : (
        <>
          <button type="button" className="pick" aria-expanded={open} onClick={() => setFlipped((state) => ({ ...state, [folder.id]: !state[folder.id] }))}>
            <i className="chev" aria-hidden="true">›</i>{FOLDER}<span className="what">{folderName(folder.id, list?.sessions?.[folder.id])}</span>{folder.id === session ? <i className="run" title="Running" /> : null}<small>{folder.items.length}</small>
          </button>
          {folder.id === session ? null : more(`session:${folder.id}`, [["Rename", () => setEditing(`session:${folder.id}`)]])}
        </>
      )}
    </div>
  );
  const pinned = items.filter((item) => item.pinned);
  return (
    <aside className="chats" aria-label="Conversations">
      <div className="rail-heading"><h3>Conversations</h3>{onNew ? <button type="button" className="icon" aria-label="New conversation" title="New conversation" disabled={busy} onClick={onNew}>+</button> : null}</div>
      <div className="chatlist">
        {pinned.length ? <><div className="grp">Pinned</div>{pinned.map(row)}</> : null}
        {groups.map(({ label, folders: listed }) => (
          <Fragment key={label}>
            <div className="grp">{label}</div>
            {listed.map((folder) => {
              const open = (folder === first || folder.items.some((item) => item.id === current || item.id === shown)) !== Boolean(flipped[folder.id]);
              return <Fragment key={folder.id}>{folderRow(folder, open)}{open ? folder.items.map(row) : null}</Fragment>;
            })}
          </Fragment>
        ))}
        {items.length ? null : <div className="empty">No conversations yet</div>}
      </div>
    </aside>
  );
}
