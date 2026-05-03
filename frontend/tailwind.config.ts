import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Helvetica', 'Arial'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
      },
      colors: {
        // Dark tactical palette — close to GitHub dark, friendly to high-contrast.
        panel: {
          950: "#070b10",
          900: "#0b1018",
          800: "#111823",
          700: "#1a2433",
          600: "#263449",
        },
        accent: {
          green: "#3fb950",
          amber: "#e3b341",
          red:   "#f85149",
          blue:  "#58a6ff",
          cyan:  "#39d0d8",
          violet:"#a371f7",
        },
      },
      boxShadow: {
        'glow-green': '0 0 18px -6px rgba(63,185,80,0.6)',
        'glow-red': '0 0 18px -6px rgba(248,81,73,0.75)',
        'glow-amber': '0 0 18px -6px rgba(227,179,65,0.6)',
      },
      animation: {
        'pulse-dot': 'pulse 1.6s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
};
export default config;
