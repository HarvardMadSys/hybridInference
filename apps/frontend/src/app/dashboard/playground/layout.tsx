'use client';

export default function PlaygroundLayout({ children }: { children: React.ReactNode }) {
  return <div className="fixed inset-0 z-50 flex flex-col bg-gray-950">{children}</div>;
}
