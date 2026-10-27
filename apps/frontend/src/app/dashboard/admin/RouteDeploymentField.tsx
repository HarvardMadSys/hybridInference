import { useT } from '@/components/providers/useT';
import type { RouteDeploymentType } from '@/lib/api/admin';
import { Select } from '@/components/ui/Select';

export function RouteDeploymentField({
  id,
  value,
  onChange,
  disabled = false,
}: {
  id: string;
  value: RouteDeploymentType;
  onChange: (value: RouteDeploymentType) => void;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <div className="mt-3">
      <label className="text-[12px] font-medium text-gray-500" htmlFor={id}>
        {t('admin.routing.deployment_type', 'Deployment type')}
      </label>
      <Select<RouteDeploymentType>
        id={id}
        value={value}
        aria-describedby={`${id}-help`}
        onValueChange={onChange}
        disabled={disabled}
        options={[
          {
            value: 'auto',
            label: t('admin.routing.deployment_auto', 'Auto (use endpoint hostname)'),
          },
          {
            value: 'self_hosted',
            label: t('admin.routing.deployment_self_hosted', 'Self-hosted engine'),
          },
          {
            value: 'provider_api',
            label: t('admin.routing.deployment_provider_api', 'Provider API'),
          },
        ]}
        size="lg"
        className="mt-1 w-full"
      />
      <p id={`${id}-help`} className="mt-1 text-[12px] text-gray-500">
        {t(
          'admin.routing.deployment_help',
          'Choose Self-hosted engine for your own inference server, including a LAN or cluster address, to use the engine queue limit. Provider API uses provider/key concurrency limits. Auto recognizes localhost addresses. This does not change the URL.',
        )}
      </p>
    </div>
  );
}
