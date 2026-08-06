import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** Surface this Markdown renders on — picks the accent, border and code palette. */
export type MarkdownTone = 'light' | 'dark';

interface Palette {
  link: string;
  blockquote: string;
  pre: string;
  inlineCode: string;
  cell: string;
}

const PALETTES: Record<MarkdownTone, Palette> = {
  light: {
    link: 'text-crimson underline underline-offset-2 hover:opacity-80',
    blockquote: 'border-gray-300 text-gray-600',
    pre: 'bg-gray-900 text-gray-100',
    inlineCode: 'bg-gray-100 text-crimson',
    cell: 'border-gray-200',
  },
  dark: {
    link: 'text-indigo-400 underline underline-offset-2 hover:text-indigo-300',
    blockquote: 'border-gray-700 text-gray-400',
    pre: 'border border-gray-800 bg-gray-950 text-gray-100',
    inlineCode: 'bg-gray-800 text-indigo-300',
    cell: 'border-gray-700',
  },
};

// Render each element explicitly with Tailwind utilities: the
// @tailwindcss/typography (`prose`) plugin isn't installed, so element defaults
// would otherwise be stripped by the CSS reset (flat lists, unsized headings).
function buildComponents(palette: Palette): Components {
  return {
    p: ({ children }) => <p className="mb-2 leading-relaxed">{children}</p>,
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noopener noreferrer" className={palette.link}>
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
      <blockquote className={`mb-2 border-l-2 pl-3 italic ${palette.blockquote}`}>
        {children}
      </blockquote>
    ),
    pre: ({ children }) => (
      <pre className={`mb-2 overflow-x-auto rounded-lg p-3 text-xs ${palette.pre}`}>{children}</pre>
    ),
    code: ({ className, children }) =>
      /language-/.test(className || '') ? (
        <code className={className}>{children}</code>
      ) : (
        <code className={`rounded px-1 py-0.5 text-[0.85em] ${palette.inlineCode}`}>
          {children}
        </code>
      ),
    table: ({ children }) => (
      <div className="mb-2 overflow-x-auto">
        <table className="w-full border-collapse text-xs">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className={`border px-2 py-1 text-left font-semibold ${palette.cell}`}>{children}</th>
    ),
    td: ({ children }) => <td className={`border px-2 py-1 ${palette.cell}`}>{children}</td>,
  };
}

const COMPONENTS: Record<MarkdownTone, Components> = {
  light: buildComponents(PALETTES.light),
  dark: buildComponents(PALETTES.dark),
};

// Hoisted so the plugin array keeps a stable identity across renders — this
// renders token-by-token on the playground's streaming path.
const REMARK_PLUGINS = [remarkGfm];

/** Render a Markdown string (GitHub-flavored) as styled HTML. */
export function Markdown({ text, tone = 'light' }: { text: string; tone?: MarkdownTone }) {
  return (
    <div className="break-words text-sm leading-relaxed [&>*:last-child]:mb-0">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS[tone]}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
