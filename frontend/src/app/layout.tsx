import type { ReactNode } from "react";

export const metadata = {
  title: "Praxis",
  description: "Praxis: Autonomous Revenue Decision Infrastructure (synthetic data)",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
