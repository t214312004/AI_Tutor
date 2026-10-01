import { describe, expect, it } from 'vitest';
import {
  defaultRoute, GptOptions, loadPreferences, ModelOption, reconcileDefaults,
  savePreferences, updatePreferences,
} from './gpt-models';

const model = (id: string, efforts = ['low', 'medium', 'max'], image = true): ModelOption =>
  ({ id, label: id, efforts, image });
const catalog = (models: ModelOption[]): GptOptions => ({
  openai: { models, error: null }, codex: { models, error: null }, agy: { models, error: null },
});

describe('GPT Live model defaults', () => {
  it('selects the newest Sol for both teaching routes and newest image-capable Luna for vision', () => {
    const options = catalog([
      model('gpt-6-astra'), model('gpt-6.9-sol'), model('gpt-6-luna'),
      model('gpt-6.10-sol'), model('gpt-6.2-luna'), model('gpt-7-luna', ['medium'], false),
    ]);
    for (const provider of ['openai', 'codex'] as const) {
      expect(defaultRoute(options, provider, false).model).toBe('gpt-6.10-sol');
      expect(defaultRoute(options, provider, true).model).toBe('gpt-6.2-luna');
    }
    expect(defaultRoute(options, 'openai', false).effort).toBe('low');
    expect(defaultRoute(options, 'openai', true).effort).toBe('max');
  });

  it('uses latest Flash and compatible medium effort for both agy roles, independent of list order', () => {
    const options = catalog([
      model('gemini-4-pro-high', ['high']), model('gemini-3.8-flash-high', ['high']),
      model('gemini-3.7-flash-medium', ['medium']), model('gemini-4-flash-lite'),
      model('gemini-3.8-flash-low', ['low']), model('gemini-3.8-flash-medium', ['medium']),
    ]);
    for (const image of [true, false]) {
      expect(defaultRoute(options, 'agy', image)).toEqual({
        provider: 'agy', model: 'gemini-3.8-flash-medium', effort: 'medium',
      });
    }
  });

  it('prefers a stable alias over snapshots or preview variants of the same version', () => {
    expect(defaultRoute(catalog([
      model('gpt-6.1-sol-2026-10-01'), model('gpt-6.1-sol'),
    ]), 'codex', false).model).toBe('gpt-6.1-sol');
    expect(defaultRoute(catalog([
      model('gemini-3.8-flash-preview'), model('gemini-3.8-flash'),
    ]), 'agy', true).model).toBe('gemini-3.8-flash');
  });

  it('completes provider defaults after asynchronous discovery and preserves compatible effort', () => {
    const old = loadPreferences('test', null);
    const pending = updatePreferences(old, { ...old.config, delegation: {
      type: 'client', provider: 'codex', model: '', effort: '',
    } });
    expect(reconcileDefaults(pending, catalog([]))).toBe(pending);
    const ready = reconcileDefaults(pending, catalog([model('gpt-6.1-sol'), model('gpt-6-luna')]));
    expect(ready.config.delegation).toEqual({
      type: 'client', provider: 'codex', model: 'gpt-6.1-sol', effort: 'medium',
    });
    expect(ready.config.vision.effort).toBe('max');
    expect(reconcileDefaults(ready, catalog([model('gpt-6.1-sol'), model('gpt-6-luna')]))).toBe(ready);
  });

  it('migrates old saved defaults once, preserves later manual choices across reload and new catalogs', () => {
    const options = catalog([model('gpt-6-astra'), model('gpt-6-sol'), model('gpt-6.1-sol'), model('gpt-6-luna')]);
    const legacy = JSON.stringify({ version: 1,
      delegation: { type: 'client', provider: 'codex', model: 'gpt-6-astra', effort: 'low' },
      vision: { provider: 'codex', model: 'gpt-6-sol', effort: 'max' },
    });
    const migrated = reconcileDefaults(loadPreferences('test', legacy), options);
    expect(migrated.config.delegation.model).toBe('gpt-6.1-sol');
    expect(migrated.config.vision.model).toBe('gpt-6-luna');
    const manual = updatePreferences(migrated, { ...migrated.config,
      delegation: { ...migrated.config.delegation, model: 'gpt-6-astra' },
    });
    const reloaded = loadPreferences('test', savePreferences(manual));
    const refreshed = reconcileDefaults(reloaded, catalog([...options.codex.models, model('gpt-7-sol'), model('gpt-7-luna')]));
    expect(refreshed.config.delegation.model).toBe('gpt-6-astra');
    expect(refreshed.config.vision.model).toBe('gpt-7-luna');
    const switched = updatePreferences(refreshed, { ...refreshed.config,
      delegation: { type: 'responses', ...defaultRoute(options, 'openai', false) },
    });
    expect(switched.modelDefaults.delegation).toBe(true);
  });

  it('does not conceal an unavailable manually selected model, and handles damaged preferences', () => {
    const defaults = loadPreferences('test', '{invalid');
    expect(defaults.config.delegation.model).toBe('gpt-6.1-sol');
    const manual = updatePreferences(defaults, { ...defaults.config,
      vision: { ...defaults.config.vision, model: 'retired-model' },
    });
    expect(reconcileDefaults(manual, catalog([model('gpt-6-luna')])).config.vision.model).toBe('retired-model');
  });
});
