import type { Metadata, Viewport } from "next";

import { MeProvider } from "@/components/me";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "CountVision", template: "%s · CountVision" },
  description: "Visitor and traffic numbers from the cameras you already have.",
  robots: { index: false, follow: false },
  icons: { icon: "/favicon.svg" },
};

export const viewport: Viewport = { themeColor: "#0e1820", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <MeProvider>{children}</MeProvider>
      </body>
    </html>
  );
}
