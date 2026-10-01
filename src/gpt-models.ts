export type GptProvider = 'openai' | 'codex' | 'agy';
export type ModelOption = { id: string; label: string; efforts: string[]; image: boolean };
export type GptOptions = Record<GptProvider, { models: ModelOption[]; error: string | null }>;
export type GptRoute = { provider: GptProvider; model: string; effort: string };
export type GptLiveConfig = {
  version: 1;
  delegation: GptRoute & { type: 'responses' | 'client' };
  vision: GptRoute;
};
export type GptPreferences = {
  student: string;
  config: GptLiveConfig;
  modelDefaults: { delegation: boolean; vision: boolean };
};

export const defaultGptConfig: GptLiveConfig = {
  version: 1,
  delegation: { type: 'responses', provider: 'openai', model: 'gpt-6.1-sol', effort: 'low' },
  vision: { provider: 'openai', model: 'gpt-6-luna', effort: 'max' },
};

function familyVersion(id: string, provider: GptProvider, image: boolean): number[] | null {
  const pattern = provider === 'agy'
    ? /^gemini-(\d+(?:\.\d+)*)-flash(?:-(?:low|medium|high|max|preview|\d{4}-\d{2}-\d{2}))*$/i
    : new RegExp(`^gpt-(\\d+(?:\\.\\d+)*)-${image ? 'luna' : 'sol'}(?:-\\d{4}-\\d{2}-\\d{2})?$`, 'i');
  const match = id.match(pattern);
  return match ? match[1].split('.').map(Number) : null;
}

function compareVersion(a: number[], b: number[]): number {
  for (let index = 0; index < Math.max(a.length, b.length); index++) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference) return difference;
  }
  return 0;
}

export function defaultRoute(options: GptOptions, provider: GptProvider, image: boolean): GptRoute {
  const preferredEffort = provider === 'openai' ? (image ? 'max' : 'low') : 'medium';
  const choices = options[provider].models.filter(model => model.efforts.length && (!image || model.image));
  const ranked = choices.flatMap(model => {
    const version = familyVersion(model.id, provider, image);
    return version ? [{ model, version }] : [];
  }).sort((a, b) => {
    const version = compareVersion(b.version, a.version);
    if (version) return version;
    // Prefer the stable alias and matching effort among variants of one version.
    const preview = Number(a.model.id.includes('-preview')) - Number(b.model.id.includes('-preview'));
    if (preview) return preview;
    const effort = Number(b.model.efforts.includes(preferredEffort)) - Number(a.model.efforts.includes(preferredEffort));
    if (effort) return effort;
    const dated = Number(/-\d{4}-\d{2}-\d{2}/.test(a.model.id)) - Number(/-\d{4}-\d{2}-\d{2}/.test(b.model.id));
    return dated || b.model.id.localeCompare(a.model.id);
  });
  const model = ranked[0]?.model ?? choices[0];
  return { provider, model: model?.id ?? '',
    effort: model?.efforts.includes(preferredEffort) ? preferredEffort : model?.efforts[0] ?? '' };
}

export function reconcileDefaults(preferences: GptPreferences, options: GptOptions): GptPreferences {
  let config = preferences.config;
  for (const role of ['delegation', 'vision'] as const) {
    if (!preferences.modelDefaults[role]) continue;
    const route = config[role];
    const next = defaultRoute(options, route.provider, role === 'vision');
    // Leave the selection intact while a provider is loading or unavailable.
    if (!next.model) continue;
    const model = options[route.provider].models.find(item => item.id === next.model)!;
    if (model.efforts.includes(route.effort)) next.effort = route.effort;
    if (route.model !== next.model || route.effort !== next.effort) {
      config = { ...config, [role]: { ...route, ...next } };
    }
  }
  return config === preferences.config ? preferences : { ...preferences, config };
}

export function updatePreferences(preferences: GptPreferences, config: GptLiveConfig): GptPreferences {
  const modelDefaults = { ...preferences.modelDefaults };
  for (const role of ['delegation', 'vision'] as const) {
    const old = preferences.config[role], next = config[role];
    const providerChanged = old.provider !== next.provider ||
      (role === 'delegation' && preferences.config.delegation.type !== config.delegation.type);
    if (providerChanged) modelDefaults[role] = true;
    else if (old.model !== next.model) modelDefaults[role] = false;
  }
  return { ...preferences, config, modelDefaults };
}

export function loadPreferences(student: string, saved: string | null): GptPreferences {
  const fallback: GptPreferences = { student, config: defaultGptConfig,
    modelDefaults: { delegation: true, vision: true } };
  try {
    const candidate = saved ? JSON.parse(saved) : null;
    const routeValid = (route: GptRoute | undefined) => route &&
      ['openai', 'codex', 'agy'].includes(route.provider) &&
      typeof route.model === 'string' && typeof route.effort === 'string';
    if (candidate?.version !== 1 || !routeValid(candidate.delegation) || !routeValid(candidate.vision) ||
      !['responses', 'client'].includes(candidate.delegation.type) ||
      (candidate.delegation.type === 'responses' ? candidate.delegation.provider !== 'openai' :
        !['codex', 'agy'].includes(candidate.delegation.provider))) return fallback;
    // Existing saved routes adopt the new defaults once. Later manual selections persist.
    const modelDefaults = candidate.defaultsVersion === 1 &&
      typeof candidate.modelDefaults?.delegation === 'boolean' &&
      typeof candidate.modelDefaults?.vision === 'boolean'
      ? candidate.modelDefaults : fallback.modelDefaults;
    return { student, config: { version: 1, delegation: candidate.delegation, vision: candidate.vision }, modelDefaults };
  } catch { return fallback; }
}

export function savePreferences(preferences: GptPreferences): string {
  return JSON.stringify({ ...preferences.config, defaultsVersion: 1, modelDefaults: preferences.modelDefaults });
}
