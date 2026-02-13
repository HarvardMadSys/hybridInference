'use client';

import { useState } from 'react';
import toast from 'react-hot-toast';
import { useApiKey, useCreateApiKey, useRegenerateApiKey } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';

export function ApiKeyManager(): JSX.Element {
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);

  const { data: apiKeyInfo, isLoading: isLoadingKey } = useApiKey();
  const createMutation = useCreateApiKey();
  const regenerateMutation = useRegenerateApiKey();

  const isLoading = createMutation.isPending || regenerateMutation.isPending;

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

  function copyToClipboard(): void {
    if (!newApiKey) return;
    void navigator.clipboard.writeText(newApiKey);
    toast.success('API Key copied to clipboard');
  }

  const maskedKey = apiKeyInfo?.key_masked;
  const hasKey = apiKeyInfo?.has_key;

  return (
    <div className="rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200">
      <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900 mb-4">
        API Key
      </h2>

      {showKey && newApiKey && (
        <div className="mb-6 rounded-lg bg-yellow-50 p-4 ring-1 ring-inset ring-yellow-200">
          <p className="text-sm font-medium text-yellow-800 mb-2">
            Important: Please save this key immediately, it will not be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
              {newApiKey}
            </code>
            <Button onClick={copyToClipboard} size="sm" variant="secondary">
              Copy
            </Button>
          </div>
        </div>
      )}

      {maskedKey && !showKey && (
        <div className="mb-4">
          <code className="block rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-800 ring-1 ring-inset ring-gray-200">
            {maskedKey}
          </code>
        </div>
      )}

      {isLoadingKey && <div className="mb-4 text-sm text-gray-500">Loading...</div>}

      <div className="flex gap-3">
        {!hasKey ? (
          <Button onClick={handleCreateKey} disabled={isLoading} isLoading={isLoading}>
            Create API Key
          </Button>
        ) : (
          <Button
            onClick={handleRegenerateKey}
            disabled={isLoading}
            isLoading={isLoading}
            variant="danger"
          >
            Regenerate
          </Button>
        )}
      </div>
    </div>
  );
}
