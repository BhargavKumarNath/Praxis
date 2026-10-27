import type { Metadata } from "next";
import { IBM_Plex_Mono, Inter, Newsreader } from "next/font/google";
import type { ReactNode } from "react";

import { Shell } from "../components/Shell";
import "./globals.css";

// Type pairing from docs/design/dashboard.md: Newsreader for view titles only,
// Inter for everything else (tabular figures, optical sizes), Plex Mono for identifiers that are code.
const newsreader = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-newsreader",
  display: "swap",
});
const inter = Inter({
  subsets: ["latin"],
  axes: ["opsz"],
  variable: "--font-inter",
  display: "swap",
});
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Praxis decision console",
  description: "Praxis: Autonomous Revenue Decision Infrastructure (synthetic data)",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${newsreader.variable} ${inter.variable} ${plexMono.variable}`}>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
