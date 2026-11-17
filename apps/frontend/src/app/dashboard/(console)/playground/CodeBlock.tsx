'use client';

import Prism from 'prismjs';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-bash';
import { useMemo } from 'react';

import type { CodeLanguage } from './api';

// Prism highlights the whole document on import unless told not to. Nothing on
// this page wants that: only the generated snippet is highlighted.
Prism.manual = true;

const GRAMMARS: Record<CodeLanguage, Prism.Grammar> = {
  python: Prism.languages.python,
  curl: Prism.languages.bash,
};

/** The generated snippet, highlighted with Prism's grammar for its language. */
export function CodeBlock({ code, language }: { code: string; language: CodeLanguage }) {
  const html = useMemo(() => Prism.highlight(code, GRAMMARS[language], language), [code, language]);
  return (
    <pre className="code-highlight whitespace-pre font-mono text-[12px] leading-5 text-gray-800">
      <code dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  );
}
