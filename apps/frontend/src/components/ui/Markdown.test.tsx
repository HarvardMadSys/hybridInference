// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Markdown } from './Markdown';

afterEach(cleanup);

describe('Markdown', () => {
  it('renders markdown as HTML rather than literal text', () => {
    render(
      <Markdown
        text={'Get a **key** from the [dashboard](https://x.test).\n\n1. one\n2. two\n3. three'}
      />,
    );

    // **key** -> <strong>, not literal asterisks
    expect(screen.getByText('key').tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*key\*\*/)).toBeNull();

    // [dashboard](url) -> anchor opening in a new tab
    const link = screen.getByRole('link', { name: 'dashboard' });
    expect(link).toHaveAttribute('href', 'https://x.test');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));

    // ordered list -> three <li>
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
  });

  it('renders inline code and fenced code blocks', () => {
    const { container } = render(
      <Markdown text={'run `npm test` first\n\n```bash\necho hi\n```'} />,
    );
    expect(container.querySelector('code')).not.toBeNull();
    expect(container.querySelector('pre')).not.toBeNull();
    expect(screen.getByText('npm test').tagName).toBe('CODE');
  });

  // The `prose` plugin is not installed, so a dark surface cannot rely on
  // `prose-invert` — the tone has to reach the elements as real utilities.
  it('carries structural utilities on both tones', () => {
    for (const tone of ['light', 'dark'] as const) {
      const { container } = render(<Markdown text={'- one\n- two'} tone={tone} />);
      expect(container.querySelector('ul')).toHaveClass('list-disc');
      cleanup();
    }
  });

  it('applies dark accents only on the dark tone', () => {
    const { container: light } = render(<Markdown text={'`x` and [a](https://x.test)'} />);
    expect(light.querySelector('code')).toHaveClass('text-crimson');
    expect(light.querySelector('a')).toHaveClass('text-crimson');
    cleanup();

    const { container: dark } = render(
      <Markdown text={'`x` and [a](https://x.test)'} tone="dark" />,
    );
    expect(dark.querySelector('code')).toHaveClass('text-indigo-300');
    expect(dark.querySelector('a')).toHaveClass('text-indigo-400');
    expect(dark.querySelector('code')).not.toHaveClass('text-crimson');
  });
});
