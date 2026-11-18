"use client";

import { type ReactNode, type SyntheticEvent, useEffect, useState } from "react";

const DOTS = <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" /></svg>;
const hold = (event: SyntheticEvent) => event.stopPropagation(); // a press inside a menu is not the press elsewhere that closes it

// A row's ⋯ button and its menu, shown on hover as in the DeepSeek harness: one open at a time in a list, closed by a
// press elsewhere or Escape. Gives the renderer, ``more(key, [[label, action], …])``, and ``show(key)`` for a right-click.
export function useMore() {
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    if (open === null) return;
    const close = (event: Event) => { if (!(event instanceof KeyboardEvent) || event.key === "Escape") setOpen(null); };
    window.addEventListener("pointerdown", close); window.addEventListener("keydown", close);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", close); };
  }, [open]);
  const more = (key: string, entries: [string, () => void][]): ReactNode => (
    <>
      <button type="button" className="more" aria-label="More" aria-expanded={open === key} onPointerDown={hold} onClick={() => setOpen(open === key ? null : key)}>{DOTS}</button>
      {open === key ? <div className="menu" role="menu" onPointerDown={hold}>{entries.map(([label, act]) => <button key={label} type="button" role="menuitem" onClick={() => { setOpen(null); act(); }}>{label}</button>)}</div> : null}
    </>
  );
  return { more, show: setOpen };
}
