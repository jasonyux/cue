import { useEffect, useRef, type RefObject } from "react";

// What counts as a control: a press on one of these never dismisses a popup, it does its own thing (the popup's own
// toggle among them, so the button that opened it still closes it).
const CONTROLS = "button, a, input, select, textarea, label, summary, [role=button], [role=menuitem], [contenteditable=true]";

// Close an open popup when the person presses anywhere outside it that is not a control: the page around it, the desk,
// the monitor. ``inside`` holds the popup's own elements, including any part drawn elsewhere (a menu hung on the stage).
export function useDismiss(open: boolean, inside: RefObject<Element | null>[], close: () => void) {
  const latest = useRef({ inside, close });
  useEffect(() => { latest.current = { inside, close }; }); // the newest refs and handler, without re-subscribing on every render
  useEffect(() => {
    if (!open) return;
    const press = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target || target.closest(CONTROLS)) return;
      if (latest.current.inside.some((ref) => ref.current?.contains(target))) return;
      latest.current.close();
    };
    document.addEventListener("pointerdown", press);
    return () => document.removeEventListener("pointerdown", press);
  }, [open]);
}
