import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep `next dev` from writing generated files into the repo.
  agentRules: false,
};

export default nextConfig;
