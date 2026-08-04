import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// `npm run dev` serves this bundle with hot reload and forwards everything the
// application actually calls to a deployment. The backend cannot run usefully
// on a laptop -- the on-behalf-of exchange needs a token only AKS projects into
// a pod -- so the dev server proxies to a real one instead of faking it.
//
//   azd env get-value APP_URL
//   CHAT_BACKEND=<that value> npm --prefix frontend run dev
//
// docs/localdev.md covers what this loop can and cannot show you. The short
// version: the session cookie is `__Host-` prefixed, so it needs a Secure
// origin, and the sign-in round trip lands on the deployed host because that is
// what `OAUTH_REDIRECT_URI` names.
const backend = process.env.CHAT_BACKEND

const proxied = ["/api", "/login", "/logout", "/auth", "/health"]

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: backend
    ? {
        proxy: Object.fromEntries(
          proxied.map((route) => [
            route,
            { target: backend, changeOrigin: true, secure: true },
          ]),
        ),
      }
    : undefined,
})
