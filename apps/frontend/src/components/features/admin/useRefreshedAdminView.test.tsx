// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  REFRESH_DID_NOT_COMPLETE,
  REFRESH_POLL_MS,
  type RefreshedAdminView,
  useRefreshedAdminView,
} from './useRefreshedAdminView';

interface View extends RefreshedAdminView {
  value: string;
}

const view = (value: string, generated_at: string | null, refreshing = false): View => ({
  value,
  generated_at,
  refreshing,
});

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** Let the pending fetch resolve, then the next poll fire. */
async function nextPoll() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(REFRESH_POLL_MS);
  });
}

it('shows a kept view at once and asks only once', async () => {
  const fetchView = vi.fn().mockResolvedValue(view('a', '2026-10-19T12:00:00Z'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load());
  await nextPoll();

  expect(result.current.data?.value).toBe('a');
  expect(result.current.busy).toBe(false);
  expect(fetchView).toHaveBeenCalledTimes(1);
  expect(fetchView).toHaveBeenCalledWith({ refresh: false });
});

it('keeps the old view while the server refreshes, then shows the new one', async () => {
  const fetchView = vi
    .fn()
    .mockResolvedValueOnce(view('old', '2026-10-19T12:00:00Z', true))
    .mockResolvedValueOnce(view('old', '2026-10-19T12:00:00Z', true))
    .mockResolvedValueOnce(view('new', '2026-10-19T12:05:00Z'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load(true));
  expect(result.current.data?.value).toBe('old');
  expect(result.current.busy).toBe(true);

  await nextPoll();
  expect(result.current.data?.value).toBe('old');
  await nextPoll();

  expect(result.current.data?.value).toBe('new');
  expect(result.current.busy).toBe(false);
  expect(result.current.error).toBeNull();
  // Only the first request asks for a refresh; the polls just read.
  expect(fetchView.mock.calls.map(([opts]) => opts.refresh)).toEqual([true, false, false]);
});

it('says so when a refresh ends without a new result', async () => {
  const fetchView = vi
    .fn()
    .mockResolvedValueOnce(view('old', '2026-10-19T12:00:00Z', true))
    .mockResolvedValueOnce(view('old', '2026-10-19T12:00:00Z'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load(true));
  await nextPoll();

  expect(result.current.data?.value).toBe('old');
  expect(result.current.error).toBe(REFRESH_DID_NOT_COMPLETE);
  expect(result.current.busy).toBe(false);
});

it('polls through a first computation that outlasts the request', async () => {
  const fetchView = vi
    .fn()
    .mockResolvedValueOnce(view('', null, true))
    .mockResolvedValueOnce(view('first', '2026-10-19T12:00:00Z'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load());
  expect(result.current.data?.generated_at).toBeNull();
  await nextPoll();

  expect(result.current.data?.value).toBe('first');
  expect(result.current.error).toBeNull();
});

it('a first computation finished within a refresh request is not a failure', async () => {
  const fetchView = vi.fn().mockResolvedValue(view('first', '2026-10-19T12:00:00Z'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load(true));

  expect(result.current.data?.value).toBe('first');
  expect(result.current.error).toBeNull();
});

it('stops polling once unmounted', async () => {
  const fetchView = vi.fn().mockResolvedValue(view('old', '2026-10-19T12:00:00Z', true));
  const { result, unmount } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load(true));
  unmount();
  await nextPoll();
  await nextPoll();

  expect(fetchView).toHaveBeenCalledTimes(1);
});

it('reports a failed request', async () => {
  const fetchView = vi.fn().mockRejectedValue(new Error('statement timeout'));
  const { result } = renderHook(() => useRefreshedAdminView(fetchView));

  await act(() => result.current.load());

  expect(result.current.error).toBe('statement timeout');
  expect(result.current.busy).toBe(false);
});
