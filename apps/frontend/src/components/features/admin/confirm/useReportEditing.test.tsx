// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useReportEditing } from './useReportEditing';

afterEach(cleanup);

function Editor({ active, report }: { active: boolean; report: (active: boolean) => void }) {
  useReportEditing(active, report);
  return null;
}

describe('useReportEditing', () => {
  it('reports each change of state, and releases the parent when the editor goes away', () => {
    const report = vi.fn();
    const { rerender, unmount } = render(<Editor active={false} report={report} />);
    expect(report).toHaveBeenLastCalledWith(false);

    rerender(<Editor active report={report} />);
    expect(report).toHaveBeenLastCalledWith(true);

    // Unmounted mid-edit (its section hidden, say): the parent must not stay locked.
    unmount();
    expect(report).toHaveBeenLastCalledWith(false);
  });

  it('works without a parent listening', () => {
    expect(() => render(<Editor active report={undefined as never} />)).not.toThrow();
  });
});
