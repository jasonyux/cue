"use client";

import { useEffect, useState, type RefObject } from "react";

// Track overflow as content loads, expands, or the pane is resized.
export function useScrollEdges(ref: RefObject<HTMLElement | null>, axis: "x" | "y", revision: unknown) {
  const [edges, setEdges] = useState({ before: false, after: false });
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => {
      const offset = axis === "x" ? element.scrollLeft : element.scrollTop;
      const size = axis === "x" ? element.clientWidth : element.clientHeight;
      const total = axis === "x" ? element.scrollWidth : element.scrollHeight;
      const before = offset > 2, after = offset + size < total - 2;
      setEdges((current) => current.before === before && current.after === after ? current : { before, after });
    };
    update();
    element.addEventListener("scroll", update, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(element);
    for (const child of element.children) observer?.observe(child);
    return () => { element.removeEventListener("scroll", update); observer?.disconnect(); };
  }, [ref, axis, revision]);
  return edges;
}
