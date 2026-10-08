import type { NextConfig } from "next";

// The browser only talks to this web server. Everything under /api is passed on to the
// FastAPI service, so login cookies stay first-party and no CORS is needed.
// API_URL is read at BUILD time (Docker build arg); default = API running on this PC.
const apiUrl = (process.env.API_URL ?? "http://127.0.0.1:8001").replace(/\/$/, "");

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default config;
