"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// The same links on every page (each page used to hand-write its own, different, list)
const LINKS = [
  { href: "/", label: "Releases & Timeline" },
  { href: "/incidents", label: "Incidents" },
  { href: "/analytics", label: "Analytics" },
  { href: "/trust", label: "Trust" },
  { href: "/usage", label: "Usage & Budgets" },
  { href: "/settings", label: "Settings" },
  { href: "/owner", label: "Owner View" },
];

const BASE = "px-3 py-1.5 text-xs rounded border transition flex items-center gap-1.5 whitespace-nowrap";
const ACTIVE = "bg-brand/20 text-teal-200 border-brand font-medium";
const IDLE = "bg-slate-800/80 hover:bg-slate-800 text-slate-300 border-slate-700";

export default function NavLinks({ openIncidents = 0 }: { openIncidents?: number }) {
  const pathname = usePathname() || "/";
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));

  return (
    <>
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          aria-current={isActive(link.href) ? "page" : undefined}
          className={`${BASE} ${isActive(link.href) ? ACTIVE : IDLE}`}
        >
          <span>{link.label}</span>
          {link.href === "/incidents" && openIncidents > 0 && (
            <span className="px-1.5 rounded-full bg-rose-500 text-white text-[10px] font-bold">{openIncidents}</span>
          )}
        </Link>
      ))}
    </>
  );
}
