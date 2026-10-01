export type Point = [number, number];
export type Rotation = 0 | 90 | 180 | 270;
export type CameraSettings = {
  version: 2;
  rotation: Rotation;
  fineRotation: number;
  corners: Point[];
  perspectiveEnabled: boolean;
  outputAspect: 'auto' | 'a4-portrait' | 'a4-landscape';
};
export const defaultCorners: Point[] = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];
export const defaultCameraSettings: CameraSettings = {
  version: 2,
  rotation: 180,
  fineRotation: 0,
  corners: defaultCorners,
  perspectiveEnabled: true,
  outputAspect: 'auto',
};

export function isFullFrameCorners(corners: Point[], tolerance = 0.02) {
  return corners.length === 4 && corners.every(([x, y], index) =>
    Math.abs(x - defaultCorners[index][0]) <= tolerance &&
    Math.abs(y - defaultCorners[index][1]) <= tolerance,
  );
}

// Inverse homography maps the rectified page back into the rotated camera frame.
function solve(a: number[][]): number[] {
  const n = a.length;
  for (let i = 0; i < n; i++) {
    let pivot = i;
    for (let j = i + 1; j < n; j++) if (Math.abs(a[j][i]) > Math.abs(a[pivot][i])) pivot = j;
    [a[i], a[pivot]] = [a[pivot], a[i]];
    if (Math.abs(a[i][i]) < 1e-9) throw new Error('校正點不可重疊或共線');
    const v = a[i][i];
    for (let j = 0; j < n; j++)
      if (j !== i) {
        const f = a[j][i];
        for (let k = i; k <= n; k++) a[j][k] -= f * a[i][k];
      }
  }
  return a.map((row) => row[n]);
}
export function homography(corners: Point[]) {
  if (
    corners.length !== 4 ||
    corners.some((p) => p.length !== 2 || p.some((n) => !Number.isFinite(n) || n < 0 || n > 1))
  )
    throw new Error('無效的校正範圍');
  const cross = corners.map((p, i) => {
    const b = corners[(i + 1) % 4],
      c = corners[(i + 2) % 4];
    return (b[0] - p[0]) * (c[1] - b[1]) - (b[1] - p[1]) * (c[0] - b[0]);
  });
  if (cross.some((n) => n <= 0.0001)) throw new Error('請依左上、右上、右下、左下順序選取範圍');
  const a: number[][] = [];
  defaultCorners.forEach(([u, v], i) => {
    const [x, y] = corners[i];
    a.push([u, v, 1, 0, 0, 0, -u * x, -v * x, x], [0, 0, 0, u, v, 1, -u * y, -v * y, y]);
  });
  return solve(a);
}

export function validateSettings(value: unknown): CameraSettings {
  if (!value || typeof value !== 'object') throw new Error('無效的相機設定');
  const v = value as CameraSettings;
  if (
    v.version !== 2 ||
    !([0, 90, 180, 270] as number[]).includes(v.rotation) ||
    !Number.isFinite(v.fineRotation) ||
    Math.abs(v.fineRotation) > 15 ||
    !['auto', 'a4-portrait', 'a4-landscape'].includes(v.outputAspect) ||
    typeof v.perspectiveEnabled !== 'boolean'
  )
    throw new Error('無效的相機設定');
  homography(v.corners);
  return v;
}

export function rotatedSize(width: number, height: number, degrees: number) {
  const radians = (degrees * Math.PI) / 180;
  return {
    width: Math.max(
      1,
      Math.ceil(Math.abs(width * Math.cos(radians)) + Math.abs(height * Math.sin(radians)) - 1e-6),
    ),
    height: Math.max(
      1,
      Math.ceil(Math.abs(width * Math.sin(radians)) + Math.abs(height * Math.cos(radians)) - 1e-6),
    ),
  };
}

type CameraSource = HTMLVideoElement | HTMLImageElement;
function sourceSize(source: CameraSource) {
  return source instanceof HTMLVideoElement
    ? { width: source.videoWidth, height: source.videoHeight }
    : { width: source.naturalWidth, height: source.naturalHeight };
}
function rotatedFrame(video: CameraSource, settings: CameraSettings, maxWidth: number) {
  const input = sourceSize(video);
  if (!input.width || !input.height || (video instanceof HTMLVideoElement && video.readyState < 2))
    throw new Error('相機尚未提供畫面');
  const radians = ((settings.rotation + settings.fineRotation) * Math.PI) / 180;
  const size = rotatedSize(
    input.width,
    input.height,
    settings.rotation + settings.fineRotation,
  );
  const scale = Math.min(1, maxWidth / size.width);
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(size.width * scale));
  canvas.height = Math.max(1, Math.round(size.height * scale));
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
  ctx.translate(canvas.width / 2, canvas.height / 2);
  ctx.rotate(radians);
  ctx.drawImage(
    video,
    (-input.width * scale) / 2,
    (-input.height * scale) / 2,
    input.width * scale,
    input.height * scale,
  );
  return canvas;
}

function distance(a: Point, b: Point, width: number, height: number) {
  return Math.hypot((a[0] - b[0]) * width, (a[1] - b[1]) * height);
}
export function outputRatio(settings: CameraSettings, width: number, height: number) {
  if (settings.outputAspect === 'a4-portrait') return 1 / Math.SQRT2;
  if (settings.outputAspect === 'a4-landscape') return Math.SQRT2;
  const c = settings.corners;
  if (!settings.perspectiveEnabled) {
    const spanX = Math.max(...c.map((p) => p[0])) - Math.min(...c.map((p) => p[0]));
    const spanY = Math.max(...c.map((p) => p[1])) - Math.min(...c.map((p) => p[1]));
    return (spanX * width) / Math.max(1, spanY * height);
  }
  const horizontal =
    (distance(c[0], c[1], width, height) + distance(c[2], c[3], width, height)) / 2;
  const vertical = (distance(c[1], c[2], width, height) + distance(c[3], c[0], width, height)) / 2;
  return horizontal / Math.max(1, vertical);
}

/** The same geometry is used for preview and photographs; only maxWidth differs. */
export function renderFrame(
  video: CameraSource,
  settings: CameraSettings,
  maxWidth = 640,
  raw = false,
  nativeSource = false,
) {
  // Rectify original camera pixels; preview and legacy JPEG may limit output width.
  const source = rotatedFrame(video, settings, nativeSource ? Infinity : maxWidth);
  if (raw) return source;
  const ratio = outputRatio(settings, source.width, source.height);
  const output = document.createElement('canvas');
  const c = settings.corners;
  const cropWidth = settings.perspectiveEnabled
    ? (distance(c[0], c[1], source.width, source.height) +
       distance(c[3], c[2], source.width, source.height)) / 2
    : (Math.max(...c.map((p) => p[0])) - Math.min(...c.map((p) => p[0]))) * source.width;
  let detailWidth = cropWidth;
  if (nativeSource && maxWidth === Infinity) {
    const longestHorizontal = settings.perspectiveEnabled
      ? Math.max(distance(c[0], c[1], source.width, source.height),
                 distance(c[3], c[2], source.width, source.height))
      : cropWidth;
    const longestVertical = settings.perspectiveEnabled
      ? Math.max(distance(c[0], c[3], source.width, source.height),
                 distance(c[1], c[2], source.width, source.height))
      : (Math.max(...c.map((p) => p[1])) - Math.min(...c.map((p) => p[1]))) * source.height;
    // Retain the preview's aspect ratio, but do not shrink either longer source edge.
    // This also preserves the taller edge when a fixed A4 aspect ratio is selected.
    detailWidth = Math.max(longestHorizontal, longestVertical * ratio);
  }
  output.width = Math.max(1, Math.round(Math.min(maxWidth, detailWidth)));
  output.height = Math.max(1, Math.round(output.width / ratio));
  const input = source
    .getContext('2d', { willReadFrequently: true })!
    .getImageData(0, 0, source.width, source.height);
  const ctx = output.getContext('2d')!;
  const out = ctx.createImageData(output.width, output.height);
  const h = settings.perspectiveEnabled ? homography(c) : null;
  const minX = Math.min(...c.map((p) => p[0])),
    maxX = Math.max(...c.map((p) => p[0]));
  const minY = Math.min(...c.map((p) => p[1])),
    maxY = Math.max(...c.map((p) => p[1]));
  for (let y = 0; y < output.height; y++)
    for (let x = 0; x < output.width; x++) {
      const u = x / Math.max(1, output.width - 1),
        v = y / Math.max(1, output.height - 1);
      const denominator = h ? h[6] * u + h[7] * v + 1 : 1;
      const sx = h ? (h[0] * u + h[1] * v + h[2]) / denominator : minX + u * (maxX - minX);
      const sy = h ? (h[3] * u + h[4] * v + h[5]) / denominator : minY + v * (maxY - minY);
      const sourceX = Math.min(source.width - 1, Math.max(0, sx * (source.width - 1)));
      const sourceY = Math.min(source.height - 1, Math.max(0, sy * (source.height - 1)));
      const x0 = Math.floor(sourceX), y0 = Math.floor(sourceY);
      const x1 = Math.min(source.width - 1, x0 + 1), y1 = Math.min(source.height - 1, y0 + 1);
      const fx = sourceX - x0, fy = sourceY - y0;
      const a = (y0 * source.width + x0) * 4;
      const b = (y0 * source.width + x1) * 4;
      const c = (y1 * source.width + x0) * 4;
      const d = (y1 * source.width + x1) * 4;
      const offset = (y * output.width + x) * 4;
      for (let channel = 0; channel < 3; channel++)
        out.data[offset + channel] =
          (input.data[a + channel] * (1 - fx) + input.data[b + channel] * fx) * (1 - fy) +
          (input.data[c + channel] * (1 - fx) + input.data[d + channel] * fx) * fy;
      out.data[offset + 3] = 255;
    }
  ctx.putImageData(out, 0, 0);
  return output;
}

export function capture(video: CameraSource, settings: CameraSettings, format: 'jpeg' | 'png' = 'jpeg') {
  // OpenAI PNG keeps the selected region's camera pixels without lossy encoding.
  const frame = renderFrame(video, settings, format === 'png' ? Infinity : 1280, false, true);
  return format === 'png' ? frame.toDataURL('image/png') : frame.toDataURL('image/jpeg', 0.85);
}

export function captureMeta(video: CameraSource, settings: CameraSettings) {
  const size = sourceSize(video);
  return {
    source_width: size.width,
    source_height: size.height,
    corners: settings.corners.map((point) => [...point]),
    rotation: settings.rotation,
    fine_rotation: settings.fineRotation,
    full_frame: isFullFrameCorners(settings.corners),
  };
}

/** Map a rectangle on the corrected preview back onto the camera's source pixels. */
export function focusCameraSettings(settings: CameraSettings, rect: [number, number, number, number]): CameraSettings {
  const [left, top, right, bottom] = rect;
  if (![left, top, right, bottom].every((v) => Number.isFinite(v) && v >= 0 && v <= 1) ||
      right - left < 0.08 || bottom - top < 0.08) throw Error('請框出較大的題目範圍');
  const h = settings.perspectiveEnabled ? homography(settings.corners) : null;
  const c = settings.corners;
  const minX = Math.min(...c.map((p) => p[0])), maxX = Math.max(...c.map((p) => p[0]));
  const minY = Math.min(...c.map((p) => p[1])), maxY = Math.max(...c.map((p) => p[1]));
  const map = (u: number, v: number): Point => {
    if (!h) return [minX + u * (maxX - minX), minY + v * (maxY - minY)];
    const d = h[6] * u + h[7] * v + 1;
    return [(h[0] * u + h[1] * v + h[2]) / d,
            (h[3] * u + h[4] * v + h[5]) / d];
  };
  return { ...settings, corners: [map(left, top), map(right, top),
    map(right, bottom), map(left, bottom)] };
}
