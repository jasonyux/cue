"use client";

import { type RefObject, useEffect, useRef } from "react";

// Pilot, the pet of the Zen theme. What it does comes from the ``data-state`` of an ancestor (v1.css): it watches
// (green antenna), hops and waves when it noticed something, wears a hard hat while the agent works, waves for
// permission, and sleeps when paused or off. Its glasses are the permissions: clear while it may read what is on screen,
// dark when the app in front is blocked or not answered yet; a count on the frame is the apps waiting for an answer, and a
// click on them opens the list. It looks at the pointer when the pointer comes near; otherwise, and when
// the pointer leaves the window (to another monitor, say), its eyes slowly go back to the screen on its left.
const NEAR = 360; // px from Pilot's eyes: closer than this, it looks at the pointer
const REST = { x: -3.4, y: -0.6 }; // where it looks otherwise: at the monitor

export type Glasses = "clear" | "dark";

export function Pet({ glasses = null, asking = 0, onGlasses, glassesRef }: {
  glasses?: Glasses | null; asking?: number; onGlasses?: () => void; glassesRef?: RefObject<SVGGElement | null>;
}) {
  const svg = useRef<SVGSVGElement>(null);
  useEffect(() => {
    const el = svg.current;
    if (!el) return;
    const gaze = (x: number, y: number, slow: boolean) => {
      el.style.setProperty("--px", `${x}px`);
      el.style.setProperty("--py", `${y}px`);
      el.style.setProperty("--pt", slow ? "1.6s" : ".25s");
    };
    let resting = false;
    const rest = () => {
      if (resting) return;
      resting = true;
      gaze(REST.x, REST.y, true);
    };
    const look = (event: MouseEvent) => {
      const r = el.getBoundingClientRect();
      const dx = event.clientX - (r.left + r.width / 2), dy = event.clientY - (r.top + r.height * 0.55);
      const d = Math.hypot(dx, dy) || 1;
      if (d > NEAR) { rest(); return; }
      resting = false;
      gaze((dx / d) * 3.6, (dy / d) * 3.2, false);
    };
    // The pointer left the window: a mouseout with nowhere to go, which every browser sends (mouseleave on the page
    // is not sent by all of them); also when the window loses focus or is hidden.
    const out = (event: MouseEvent) => { if (!event.relatedTarget) rest(); };
    const hidden = () => { if (document.hidden) rest(); };
    rest();
    window.addEventListener("mousemove", look);
    document.addEventListener("mouseout", out);
    window.addEventListener("blur", rest);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      window.removeEventListener("mousemove", look);
      document.removeEventListener("mouseout", out);
      window.removeEventListener("blur", rest);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, []);
  return (
    <svg ref={svg} className="pet" viewBox="0 0 120 120" aria-label="Pilot">
      <ellipse cx="60" cy="113" rx="30" ry="4.5" fill="rgba(0,0,0,.13)" />
      <g className="hop">
        <line x1="60" y1="31" x2="60" y2="16" stroke="var(--pet-dark)" strokeWidth="4" strokeLinecap="round" />
        <circle className="bulb" cx="60" cy="12" r="6" />
        <path className="arm l" d="M24 78 q-11 5 -9 16" stroke="var(--pet-dark)" strokeWidth="7" strokeLinecap="round" fill="none" />
        <path className="arm r" d="M96 78 q11 -4 13 -16" stroke="var(--pet-dark)" strokeWidth="7" strokeLinecap="round" fill="none" />
        <ellipse cx="45" cy="106" rx="10" ry="5.5" fill="var(--pet-dark)" /><ellipse cx="75" cy="106" rx="10" ry="5.5" fill="var(--pet-dark)" />
        <path d="M20 72 C20 44 38 29 60 29 C82 29 100 44 100 72 C100 97 84 107 60 107 C36 107 20 97 20 72 Z" fill="var(--pet)" />
        <path d="M34 50 C40 40 50 36 58 36" stroke="#fff" strokeOpacity=".45" strokeWidth="5" strokeLinecap="round" fill="none" />
        <g className="hat"><path d="M28 50 C30 27 90 27 92 50 Z" fill="#ffd23f" /><rect x="24" y="47" width="72" height="7" rx="3.5" fill="#f0b400" /><rect x="56" y="30" width="8" height="18" rx="3" fill="#f0b400" /></g>
        <g className="eyes open">
          <ellipse cx="46" cy="67" rx="9.5" ry="10.5" fill="#fff" /><ellipse cx="74" cy="67" rx="9.5" ry="10.5" fill="#fff" />
          <circle className="pupil" cx="46" cy="68" r="4.8" fill="#1d1f24" /><circle className="pupil" cx="74" cy="68" r="4.8" fill="#1d1f24" />
        </g>
        <g className="eyes shut">
          <path d="M37 68 q9 7 18 0" stroke="#1d1f24" strokeWidth="3" fill="none" strokeLinecap="round" />
          <path d="M65 68 q9 7 18 0" stroke="#1d1f24" strokeWidth="3" fill="none" strokeLinecap="round" />
        </g>
        {glasses ? (
          <g ref={glassesRef} className={`glasses ${glasses}`} role="button" aria-label="Permission questions" onClick={(event) => { event.stopPropagation(); onGlasses?.(); }}>
            <title>{asking ? `${asking} app${asking === 1 ? "" : "s"} waiting for your answer` : glasses === "dark" ? "Not reading this app · permissions" : "Reading this app · permissions"}</title>
            <path className="frame" d="M34 64 L24 60 M86 64 L96 60 M58 64 q2 -3 4 0" />
            <rect className="lens" x="34" y="55" width="24" height="24" rx="8" /><rect className="lens" x="62" y="55" width="24" height="24" rx="8" />
            <path className="gloss" d="M39 61 l6 -2 M67 61 l6 -2" />
            {asking ? <><circle cx="88" cy="55" r="7.5" fill="#d64545" /><text x="88" y="58.2" textAnchor="middle" fontSize="9" fontWeight="700" fill="#fff" fontFamily="-apple-system,sans-serif">{asking}</text></> : null}
          </g>
        ) : null}
        <ellipse cx="33" cy="82" rx="6" ry="3.5" fill="#ff5f5f" opacity=".32" /><ellipse cx="87" cy="82" rx="6" ry="3.5" fill="#ff5f5f" opacity=".32" />
        <path className="mouth" d="M54 85 q6 5 12 0" stroke="#1d1f24" strokeWidth="2.6" fill="none" strokeLinecap="round" />
      </g>
    </svg>
  );
}
