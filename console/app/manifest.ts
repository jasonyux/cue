import type { MetadataRoute } from "next";

// The web app manifest, served at /manifest.webmanifest and linked from every page: it lets a browser install the
// console as a standalone app (the Install icon in Chrome's or Vivaldi's address bar, Add to Dock in Safari) under
// this name and icon, in its own window without tabs.
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Cue Console",
    short_name: "Cue",
    start_url: "/",
    display: "standalone",
    background_color: "#171c27",
    theme_color: "#171c27",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
