import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Render each element explicitly with Tailwind utilities: the
// @tailwindcss/typography (`prose`) plugin isn't installed, so element defaults
// would otherwise be stripped by the CSS reset (flat lists, unsized headings).
const components: Components = {
  p: ({ children }) => <p className="mb-2 leading-relaxed">{children}</p>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-crimson underline underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  h1: ({ children }) => <h3 className="mb-2 mt-1 text-base font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-2 mt-1 text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1 mt-1 text-sm font-semibold">{children}</h4>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-gray-300 pl-3 italic text-gray-600">
      {children}
    </blockquote>
  ),
  pre: ({ children }) => (
    <pre className="mb-2 overflow-x-auto rounded-lg bg-gray-900 p-3 text-xs text-gray-100">
      {children}
    </pre>
  ),
  code: ({ className, children }) =>
    /language-/.test(className || '') ? (
      <code className={className}>{children}</code>
    ) : (
      <code className="rounded bg-gray-100 px-1 py-0.5 text-[0.85em] text-crimson">{children}</code>
    ),
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-gray-200 px-2 py-1 text-left font-semibold">{children}</th>
  ),
  td: ({ children }) => <td className="border border-gray-200 px-2 py-1">{children}</td>,
};

/** Render a Markdown string (GitHub-flavored) as styled HTML. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="break-words text-sm leading-relaxed [&>*:last-child]:mb-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
