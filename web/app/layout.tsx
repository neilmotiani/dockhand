import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";

import { Nav } from "@/components/Nav";
import { TriggerButton } from "@/components/TriggerButton";

import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Dockhand",
  description: "Automatic receiving clerk for a Ring camera at the back door.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand">
              <span className="brand-mark" aria-hidden>
                ▣
              </span>
              Dockhand
            </Link>
            <Nav />
            <TriggerButton />
          </div>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
