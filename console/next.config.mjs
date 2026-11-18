import { fileURLToPath } from "node:url";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants.js";

/** @type {import('next').NextConfig} */
const nextConfig = {
  eslint: {
    ignoreDuringBuilds: true,
  },
  transpilePackages: ["@cue/contracts"],
  outputFileTracingRoot: fileURLToPath(new URL("..", import.meta.url)),
  devIndicators: false, // no floating "N" badge from the dev server; build errors still show as an overlay
};

// A production build clears its output directory; keep a running dev server separate.
/** @param {string} phase @returns {import('next').NextConfig} */
export default function configureNext(phase) {
  return {
    ...nextConfig,
    distDir: phase === PHASE_DEVELOPMENT_SERVER ? ".next-dev" : ".next",
  };
}
