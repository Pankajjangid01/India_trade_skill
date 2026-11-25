/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        slate: {
          900: "#0f172a",
          950: "#020617",
        },
        indian: {
          green: "#009245",
          saffron: "#FF9933",
          white: "#FFFFFF",
        },
      },
      animation: {
        "ping-ring": "ping-ring 1.5s cubic-bezier(0, 0, 0.2, 1) infinite",
      },
      keyframes: {
        "ping-ring": {
          "75%, 100%": {
            transform: "scale(2)",
            opacity: "0",
          },
        },
      },
    },
  },
  plugins: [],
}
