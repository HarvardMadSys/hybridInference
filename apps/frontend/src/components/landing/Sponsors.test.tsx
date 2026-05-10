// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Sponsors } from './Sponsors';

describe('Sponsors', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the sponsors heading and both sponsor logos', () => {
    render(<Sponsors />);

    expect(screen.getByText('Sponsors')).toBeInTheDocument();
    expect(screen.getByAltText('NVIDIA logo')).toBeInTheDocument();
    expect(screen.getByAltText('Harvard SEAS logo')).toBeInTheDocument();
  });
});
