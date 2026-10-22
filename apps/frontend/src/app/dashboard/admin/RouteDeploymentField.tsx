import { useT } from '@/components/providers/useT';
import type { RouteDeploymentType } from '@/lib/api/admin';

export function RouteDeploymentField({
  id,
  value,
  onChange,
}: {
  id: string;
  value: RouteDeploymentType;
  onChange: (value: RouteDeploymentType) => void;
}) {
  const t = useT();
  return (
    <div className="mt-3">
      <label className="text-[12px] font-medium text-gray-500" htmlFor={id}>
        {t('admin.routing.deployment_type', 'Deployment type')}
      </label>
      <select
        id={id}
        value={value}
        aria-describedby={`${id}-help`}
        onChange={(event) => onChange(event.target.value as RouteDeploymentType)}
        className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
      >
        <option value="auto">
          {t('admin.routing.deployment_auto', 'Auto (use endpoint hostname)')}
        </option>
        <option value="self_hosted">
          {t('admin.routing.deployment_self_hosted', 'Self-hosted engine')}
        </option>
        <option value="provider_api">
          {t('admin.routing.deployment_provider_api', 'Provider API')}
        </option>
      </select>
      <p id={`${id}-help`} className="mt-1 text-[12px] text-gray-500">
        {t(
          'admin.routing.deployment_help',
          'Choose Self-hosted engine for your own inference server, including a LAN or cluster address, to use the engine queue limit. Provider API uses provider/key concurrency limits. Auto recognizes localhost addresses. This does not change the URL.',
        )}
      </p>
    </div>
  );
}
