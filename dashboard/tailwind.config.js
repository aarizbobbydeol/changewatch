/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        safe: "#10b981",
        watch: "#f59e0b",
        rollback: "#ef4444",
        nodata: "#6b7280",
      },
    },
  },
  plugins: [],
};
