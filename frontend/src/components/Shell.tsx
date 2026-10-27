"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

// Views named by the question the operator brings to them; URLs keep the plan's names.
const VIEWS = [
  { href: "/executive", name: "Executive", question: "Is the business on track?" },
  { href: "/pricing", name: "Pricing", question: "What would the optimiser change?" },
  { href: "/payments", name: "Payments", question: "Are failed payments recovering?" },
  { href: "/mlops", name: "ML operations", question: "Can the models be trusted?" },
  { href: "/system", name: "System", question: "Is the platform healthy?" },
  { href: "/operator", name: "AI operator", question: "What needs a decision?" },
];

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname() ?? "/";
  return (
    <div className="shell">
      <nav className="rail" aria-label="Views">
        <Link href="/executive" className="mark">
          Praxis
          <small>Decision console</small>
        </Link>
        <ul className="nav">
          {VIEWS.map((v) => {
            const current = path === v.href || path.startsWith(`${v.href}/`);
            return (
              <li key={v.href}>
                <Link href={v.href} aria-current={current ? "page" : undefined}>
                  {v.name}
                  <span className="question">{v.question}</span>
                </Link>
              </li>
            );
          })}
        </ul>
        <p className="rail-foot">
          Every figure is SYNTHETIC simulator output. Local only; no live payments, prices or
          LLM calls.
        </p>
      </nav>
      <main className="main" id="main">
        {children}
      </main>
    </div>
  );
}
