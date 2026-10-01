import { describe, expect, it, vi } from 'vitest';
import {
  defaultCameraSettings,
  defaultCorners,
  homography,
  isFullFrameCorners,
  focusCameraSettings,
  outputRatio,
  rotatedSize,
  validateSettings,
} from './camera';
import { loadCameraSettings, saveCameraSettings } from './camera-settings';

describe('camera geometry', () => {
  it('keeps exact dimensions at right-angle rotations', () => {
    expect(rotatedSize(1920, 1080, 0)).toEqual({ width: 1920, height: 1080 });
    expect(rotatedSize(1920, 1080, 90)).toEqual({ width: 1080, height: 1920 });
    expect(rotatedSize(1920, 1080, 180)).toEqual({ width: 1920, height: 1080 });
    expect(rotatedSize(1920, 1080, 270)).toEqual({ width: 1080, height: 1920 });
  });

  it('maps a full frame to itself and rejects crossed or collapsed corners', () => {
    const identity = homography(defaultCorners);
    expect(identity.map((n) => Math.round(n * 1e6) / 1e6)).toEqual([1, 0, 0, 0, 1, 0, 0, 0]);
    expect(() =>
      homography([
        [0, 0],
        [1, 1],
        [1, 0],
        [0, 1],
      ]),
    ).toThrow();
    expect(() =>
      homography([
        [0, 0],
        [0, 0],
        [1, 1],
        [0, 1],
      ]),
    ).toThrow();
  });
  it('maps a selected question to source corners without resetting the page calibration', () => {
    const page={...defaultCameraSettings,corners:[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]] as [number,number][]};
    const focused=focusCameraSettings(page,[0.25,0.25,0.75,0.75]);
    expect(focused.corners[0][0]).toBeCloseTo(0.3);
    expect(focused.corners[0][1]).toBeCloseTo(0.3);
    expect(focused.corners[2][0]).toBeCloseTo(0.7);
    expect(focused.corners[2][1]).toBeCloseTo(0.7);
    expect(page.corners[0]).toEqual([0.1,0.1]);
    expect(isFullFrameCorners(defaultCorners)).toBe(true);
    expect(isFullFrameCorners(page.corners)).toBe(false);
  });

  it('keeps uncropped aspect ratios and validates fine rotation limits', () => {
    expect(outputRatio(defaultCameraSettings, 1920, 1080)).toBeCloseTo(16 / 9);
    expect(
      outputRatio({ ...defaultCameraSettings, outputAspect: 'a4-portrait' }, 1920, 1080),
    ).toBeCloseTo(1 / Math.SQRT2);
    expect(() => validateSettings({ ...defaultCameraSettings, fineRotation: 16 })).toThrow();
  });
  it('preserves narrow vertical question and wide line aspect ratios', () => {
    const narrow = { ...defaultCameraSettings, corners: [[0,0],[0.1,0],[0.1,1],[0,1]] as [number,number][] };
    expect(outputRatio(narrow, 1920, 1080)).toBeCloseTo(192 / 1080);
    const wide = { ...defaultCameraSettings, corners: [[0,0],[1,0],[1,0.1],[0,0.1]] as [number,number][] };
    expect(outputRatio(wide, 1920, 1080)).toBeCloseTo(1920 / 108);
  });
});

describe('camera settings', () => {
  it('migrates old four-corner calibration once and separates camera resolutions', () => {
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
      removeItem: (key: string) => {
        values.delete(key);
      },
    });
    const oldCorners: [number, number][] = [
      [0.1, 0.1],
      [0.9, 0.1],
      [0.8, 0.9],
      [0.2, 0.9],
    ];
    values.set('camera-corners', JSON.stringify(oldCorners));
    const migrated = loadCameraSettings('first', 1920, 1080);
    expect(migrated.corners).toEqual(oldCorners);
    expect(values.has('camera-corners')).toBe(false);
    expect(loadCameraSettings('second', 1920, 1080).corners).toEqual(defaultCorners);
    expect(loadCameraSettings('first', 1280, 720).corners).toEqual(defaultCorners);
    saveCameraSettings('first', 1920, 1080, { ...migrated, rotation: 90 });
    expect(loadCameraSettings('first', 1920, 1080).rotation).toBe(90);
    vi.unstubAllGlobals();
  });
});
