import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep `next dev` from writing generated files into the repo.
  agentRules: false,
  // No dev-mode badge in the corner of screen recordings.
  devIndicators: false,
  // `npm run start:demo` builds into its own folder, so a production build for recording
  // never overwrites the `.next` of a dev server that is still running.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
