/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Backend lives on port 8765. We proxy both REST and WebSocket so the
  // dashboard can call /api/* and /ws relative paths regardless of host.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8765/api/:path*" },
      { source: "/ws", destination: "http://127.0.0.1:8765/ws" },
    ];
  },
};
export default nextConfig;
