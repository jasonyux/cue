"use client";

import { type CSSProperties, useEffect, useState } from "react";

const REST = 600; // ms the pointer stays on an element before its tooltip shows

// Tooltips drawn by the page. The browser's own, from a title attribute, cost the pointer its hand: in Vivaldi on macOS
// it flickers to an arrow as the mouse moves over a control that has one. So while the pointer is over an element, its
// title and its ancestors' wait in data-tip, where the browser does not look, and go back when the pointer leaves; the
// nearest one shows here. The Zen theme is left as it was.
export function Tips() {
  const [tip, setTip] = useState<{ text: string; place: CSSProperties } | null>(null);
  useEffect(() => {
    const held = new Set<Element>(); // the elements whose title is set aside
    let host: Element | null = null; // the one whose tooltip is up or due
    let timer: ReturnType<typeof setTimeout> | undefined;
    const hide = () => { clearTimeout(timer); setTip(null); };
    const show = (from: Element) => {
      const box = from.getBoundingClientRect();
      const left = Math.max(8, Math.min(box.left, window.innerWidth - 340));
      const place = box.top > window.innerHeight / 2 ? { left, bottom: window.innerHeight - box.top + 6 } : { left, top: box.bottom + 6 };
      setTip({ text: from.getAttribute("data-tip") ?? "", place });
    };
    const enter = (target: Element | null) => {
      for (const element of held) {
        if (target && element.contains(target)) continue;
        if (!element.hasAttribute("title")) element.setAttribute("title", element.getAttribute("data-tip") ?? ""); // unless the page gave it a new one meanwhile
        element.removeAttribute("data-tip");
        held.delete(element);
      }
      for (let element = target?.closest("[title]"); element; element = element.parentElement?.closest("[title]")) {
        element.setAttribute("data-tip", element.getAttribute("title") ?? "");
        element.removeAttribute("title");
        held.add(element);
      }
      const next = target?.closest("[data-tip]") ?? null;
      if (next === host) return;
      hide();
      host = next;
      if (next?.getAttribute("data-tip")) timer = setTimeout(() => show(next), REST);
    };
    const over = (event: MouseEvent) => enter(event.target instanceof Element && !event.target.closest(".zen") ? event.target : null);
    const out = (event: MouseEvent) => { if (!event.relatedTarget) enter(null); }; // the pointer left the window
    const off = new AbortController();
    const options = { capture: true, signal: off.signal };
    document.addEventListener("mouseover", over, options);
    document.addEventListener("mouseout", out, options);
    for (const gone of ["mousedown", "keydown", "scroll"]) document.addEventListener(gone, hide, options);
    return () => { off.abort(); enter(null); };
  }, []);
  return tip ? <div className="v1tip" role="tooltip" style={tip.place}>{tip.text}</div> : null;
}
