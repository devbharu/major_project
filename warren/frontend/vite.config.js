import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production builds get a strict Content-Security-Policy (dev needs inline scripts for hot reload, so it is skipped there).
const csp = [
  "default-src 'self'", "script-src 'self'", "style-src 'self' https://fonts.googleapis.com", "style-src-attr 'unsafe-inline'",
  "font-src https://fonts.gstatic.com", "img-src 'self' data: https://github.com https://avatars.githubusercontent.com https://api.dicebear.com",
  "connect-src 'self'", "object-src 'none'", "base-uri 'none'", "form-action 'self'",
].join("; ");

const securityMeta = () => ({
  name: "warren-csp",
  transformIndexHtml: { order: "post", handler: (html, ctx) => (ctx.bundle ? html.replace("<head>", `<head>\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />`) : html) },
});

// /api is proxied to the Flask backend so the browser only talks to one origin in development.
export default defineConfig({
  plugins: [react(), securityMeta()],
  server: { port: 5173, proxy: { "/api": "http://localhost:3000" } },
  build: { sourcemap: false, target: "es2020" },
});
