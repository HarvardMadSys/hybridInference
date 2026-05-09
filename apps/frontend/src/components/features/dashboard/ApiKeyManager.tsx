'use client';

import { useState } from 'react';
import toast from 'react-hot-toast';
import { useApiKeys, useCreateApiKey, useDeleteApiKey, useRegenerateApiKey } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';

function formatDate(value?: string | null): string {
  if (!value) return 'Never';
  return new Date(value).toLocaleString();
}

function statusClassName(status: string): string {
  if (status === 'active') {
    return 'bg-green-50 text-green-700 ring-green-200';
  }
  if (status === 'revoked') {
    return 'bg-gray-50 text-gray-600 ring-gray-200';
  }
  return 'bg-yellow-50 text-yellow-700 ring-yellow-200';
}

export function ApiKeyManager(): JSX.Element {
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);
  const [deletingKeyPrefix, setDeletingKeyPrefix] = useState<string | null>(null);

  const { data: apiKeysResponse, isLoading: isLoadingKeys, error: apiKeysError } = useApiKeys();
  const createMutation = useCreateApiKey();
  const deleteMutation = useDeleteApiKey();
  const regenerateMutation = useRegenerateApiKey();

  const isLoading =
    createMutation.isPending || deleteMutation.isPending || regenerateMutation.isPending;

  async function handleCreateKey(): Promise<void> {
    if (!window.confirm('Create a new API Key?')) return;

    try {
      const response = await createMutation.mutateAsync();
      setNewApiKey(response.api_key);
      setShowKey(true);
      toast.success('API Key created successfully! Please save it now.');
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  async function handleRegenerateKey(): Promise<void> {
    if (!window.confirm('Regenerate API Key? The old key will be immediately invalidated.')) return;

    try {
      const response = await regenerateMutation.mutateAsync();
      setNewApiKey(response.api_key);
      setShowKey(true);
      toast.success('API Key regenerated successfully! Please save it now.');
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  async function handleDeleteKey(keyPrefix: string, status: string): Promise<void> {
    const message =
      status === 'active'
        ? 'Delete this API Key? It will stop working immediately.'
        : 'Remove this revoked API Key from the dashboard?';
    if (!window.confirm(message)) return;

    try {
      setDeletingKeyPrefix(keyPrefix);
      await deleteMutation.mutateAsync(keyPrefix);
      setNewApiKey(null);
      setShowKey(false);
      toast.success(status === 'active' ? 'API Key deleted successfully.' : 'API Key removed.');
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setDeletingKeyPrefix(null);
    }
  }

  const apiKeys = apiKeysResponse?.keys ?? [];
  const activeKeys = apiKeys.filter((key) => key.status === 'active');
  const hasActiveKey = activeKeys.length > 0;

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900">
            API Keys
          </h2>
          <p className="mt-1 text-sm text-gray-500">
            Manage keys used to authenticate requests to the FreeInference API.
          </p>
        </div>
        {!isLoadingKeys && !hasActiveKey && (
          <Button
            onClick={handleCreateKey}
            disabled={isLoading}
            isLoading={createMutation.isPending}
          >
            Create API Key
          </Button>
        )}
      </div>

      {showKey && newApiKey && (
        <div className="mb-6 rounded-lg bg-yellow-50 p-4 ring-1 ring-inset ring-yellow-200">
          <p className="text-sm font-medium text-yellow-800 mb-2">
            Save this key now. It will only be shown once.
          </p>
          <code className="break-all rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
            {newApiKey}
          </code>
        </div>
      )}

      {apiKeysError && (
        <div className="mb-4 rounded-md bg-red-50 px-4 py-3 text-sm text-red-700 ring-1 ring-inset ring-red-200">
          Failed to load API keys. Please try again later.
        </div>
      )}

      {isLoadingKeys && <div className="mb-4 text-sm text-gray-500">Loading...</div>}

      {!isLoadingKeys && !apiKeysError && apiKeys.length === 0 && (
        <div className="rounded-lg bg-gray-50 px-4 py-5 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
          No API keys yet. Create one to start using the API.
        </div>
      )}

      {apiKeys.length > 0 && (
        <div className="overflow-hidden rounded-lg ring-1 ring-gray-200">
          <div className="hidden grid-cols-[minmax(0,1.5fr)_auto_minmax(0,1fr)_minmax(0,1fr)_auto] gap-4 bg-gray-50 px-4 py-3 text-xs font-medium uppercase tracking-wide text-gray-500 md:grid">
            <span>API Key</span>
            <span>Status</span>
            <span>Created</span>
            <span>Last Used</span>
            <span className="text-right">Actions</span>
          </div>
          <div className="divide-y divide-gray-200 bg-white">
            {apiKeys.map((apiKey) => (
              <div
                key={apiKey.key_prefix}
                className="grid gap-3 px-4 py-4 md:grid-cols-[minmax(0,1.5fr)_auto_minmax(0,1fr)_minmax(0,1fr)_auto] md:items-center"
              >
                <div>
                  <code className="block break-all rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
                    {apiKey.api_key ?? apiKey.key_masked}
                  </code>
                  {!apiKey.api_key && (
                    <p className="mt-1 text-xs text-gray-500">
                      Full key is only shown immediately after creation or regeneration.
                    </p>
                  )}
                </div>
                <span
                  className={`w-fit rounded-full px-2.5 py-1 text-xs font-medium capitalize ring-1 ring-inset ${statusClassName(
                    apiKey.status,
                  )}`}
                >
                  {apiKey.status}
                </span>
                <div className="text-sm text-gray-600">
                  <span className="md:hidden font-medium text-gray-500">Created: </span>
                  {formatDate(apiKey.created_at)}
                </div>
                <div className="text-sm text-gray-600">
                  <span className="md:hidden font-medium text-gray-500">Last used: </span>
                  {formatDate(apiKey.last_used_at)}
                </div>
                <div className="flex gap-2 md:justify-end">
                  {apiKey.status === 'active' ? (
                    <Button
                      onClick={() => void handleDeleteKey(apiKey.key_prefix, apiKey.status)}
                      disabled={isLoading}
                      isLoading={deletingKeyPrefix === apiKey.key_prefix}
                      size="sm"
                      variant="danger"
                    >
                      Delete
                    </Button>
                  ) : apiKey.status === 'revoked' ? (
                    <Button
                      onClick={() => void handleDeleteKey(apiKey.key_prefix, apiKey.status)}
                      disabled={isLoading}
                      isLoading={deletingKeyPrefix === apiKey.key_prefix}
                      size="sm"
                      variant="subtle"
                    >
                      Remove
                    </Button>
                  ) : (
                    <span className="text-sm text-gray-400">No actions</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {hasActiveKey && (
        <div className="mt-4 flex gap-3">
          <Button
            onClick={handleRegenerateKey}
            disabled={isLoading}
            isLoading={regenerateMutation.isPending}
            variant="danger"
          >
            Regenerate
          </Button>
        </div>
      )}
    </div>
  );
}
