import Link from "next/link";

// The product's mark and name, as on the poster: a cursor in a viewfinder on a purple tile, a green dot at its
// corner. The same drawing is app/icon.svg, the tab's icon, and public/icon-192.png and icon-512.png, made of it
// (rsvg-convert -w 192 -h 192 app/icon.svg -o public/icon-192.png).
export function Mark() {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true">
      <rect width="64" height="64" rx="15" fill="#7957C7" />
      <g transform="translate(12 12) scale(1.667)" fill="none" stroke="#FFFFFF" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 7V5a2 2 0 0 1 2-2h2" />
        <path d="M17 3h2a2 2 0 0 1 2 2v2" />
        <path d="M21 17v2a2 2 0 0 1-2 2h-2" />
        <path d="M7 21H5a2 2 0 0 1-2-2v-2" />
      </g>
      <g transform="translate(22 22) scale(.92)" fill="#FFFFFF" stroke="#FFFFFF" strokeWidth=".6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z" />
      </g>
      <circle cx="47" cy="47" r="4.1" fill="#7DDABF" />
    </svg>
  );
}

export function Brand() {
  return (
    <Link className="brand" href="/v1">
      <Mark />
      Cue
    </Link>
  );
}
