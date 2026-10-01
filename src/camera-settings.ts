import { CameraSettings, defaultCameraSettings, Point, validateSettings } from './camera';

const KEY = 'camera-settings-v2';
const DEVICE_KEY = 'camera-device-id';

function settingKey(deviceId: string, width: number, height: number) {
  return `${deviceId || 'default'}:${width}x${height}`;
}

export function preferredCameraDevice() {
  return localStorage.getItem(DEVICE_KEY) ?? '';
}

export function saveCameraSettings(
  deviceId: string,
  width: number,
  height: number,
  settings: CameraSettings,
) {
  validateSettings(settings);
  let all: Record<string, CameraSettings> = {};
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) ?? '{}');
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) all = parsed;
  } catch {
    /* discard damaged index */
  }
  all[settingKey(deviceId, width, height)] = settings;
  localStorage.setItem(KEY, JSON.stringify(all));
  if (deviceId) localStorage.setItem(DEVICE_KEY, deviceId);
}

export function loadCameraSettings(
  deviceId: string,
  width: number,
  height: number,
): CameraSettings {
  const key = settingKey(deviceId, width, height);
  try {
    const all = JSON.parse(localStorage.getItem(KEY) ?? '{}');
    if (all[key]) return validateSettings(all[key]);
  } catch {
    /* use defaults */
  }
  try {
    const legacy = JSON.parse(localStorage.getItem('camera-corners') ?? 'null') as Point[] | null;
    if (legacy) {
      const migrated = validateSettings({ ...defaultCameraSettings, corners: legacy });
      saveCameraSettings(deviceId, width, height, migrated);
      localStorage.removeItem('camera-corners');
      return migrated;
    }
  } catch {
    /* use defaults */
  }
  return {
    ...defaultCameraSettings,
    corners: defaultCameraSettings.corners.map((p) => [...p] as Point),
  };
}
