import { useEffect, useState } from "react";

import { api } from "../../lib/api";
import { Button } from "../../components/ui";

interface Provider {
  name: string;
  display_name: string;
  authorize_path: string;
}

/**
 * Sign-in buttons for whichever SSO providers the deployment has configured.
 *
 * The list comes from the API rather than being hard-coded, because the API
 * only reports providers whose credentials are actually set. A self-hosted
 * install without a Google client should not be shown a Google button that
 * dead-ends in a 500.
 */
export function SsoButtons() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .get<Provider[]>("/auth/sso/providers", { signal: controller.signal })
      .then(setProviders)
      // A failure here is not worth interrupting sign-in for: the password
      // form above still works.
      .catch(() => setProviders([]));
    return () => controller.abort();
  }, []);

  if (providers.length === 0) return null;

  async function start(provider: Provider) {
    setPending(provider.name);
    try {
      const { authorization_url } = await api.get<{ authorization_url: string }>(
        provider.authorize_path,
      );
      window.location.assign(authorization_url);
    } catch {
      setPending(null);
    }
  }

  return (
    <div className="mt-6">
      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-ink-700/70" />
        <span className="text-xs text-ink-500">or continue with</span>
        <span className="h-px flex-1 bg-ink-700/70" />
      </div>

      <div className="mt-4 grid gap-2">
        {providers.map((provider) => (
          <Button
            key={provider.name}
            variant="secondary"
            full
            loading={pending === provider.name}
            onClick={() => start(provider)}
          >
            {provider.display_name}
          </Button>
        ))}
      </div>
    </div>
  );
}
