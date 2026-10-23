import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ChangeWatch — Release Anomaly Detection & Dashboard",
  description: "Deterministic release anomaly verdicts, root-cause analysis, and timeline monitoring",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="antialiased min-h-screen bg-slate-950 text-slate-100">
        {children}
      </body>
    </html>
  );
}
