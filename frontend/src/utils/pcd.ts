/**
 * Minimal PCD (Point Cloud Data v0.7) reader for the playback lidar cell.
 *
 * Handles `DATA ascii` and `DATA binary` with any field layout: x/y/z and the
 * optional PCL-packed rgb are located through the header's
 * FIELDS/SIZE/TYPE/COUNT table, so extra fields (intensity, ring, timestamp…)
 * are skipped by offset rather than rejected. `binary_compressed` is not
 * supported - LZF decoding is out of scope for a viewer cell.
 *
 * Output is capped at MAX_POINTS by striding: a 2M-point sweep rasterized into
 * a ~300px cell gains nothing from the points that never get their own pixel.
 */

export interface ParsedPcd {
  /** xyz triples in scene units (metres). */
  points: Float32Array;
  /** rgb triples (0-255), or null when the cloud carries no rgb field. */
  colors: Uint8Array | null;
}

const MAX_POINTS = 200_000;
/** A PCD header is ~10 short lines; anything past this is not a header. */
const MAX_HEADER_BYTES = 65536;

type FieldType = "I" | "U" | "F";

interface FieldSpec {
  size: number;
  type: FieldType;
  /** Byte offset of the field inside one binary point record. */
  byteOffset: number;
  /** Token offset of the field inside one ascii point line. */
  tokenOffset: number;
}

/** Byte-per-char decode of the (ASCII) header so string indices stay equal to
 *  byte offsets - the binary body starts exactly after the DATA line. */
function headerText(bytes: Uint8Array): string {
  const n = Math.min(bytes.length, MAX_HEADER_BYTES);
  let out = "";
  for (let i = 0; i < n; i += 4096) {
    out += String.fromCharCode(...bytes.subarray(i, Math.min(n, i + 4096)));
  }
  return out;
}

interface Header {
  meta: Record<string, string[]>;
  bodyOffset: number;
  dataMode: string;
}

function parseHeader(text: string): Header {
  const meta: Record<string, string[]> = {};
  let offset = 0;
  let dataMode = "";
  while (offset < text.length) {
    let nl = text.indexOf("\n", offset);
    if (nl < 0) nl = text.length;
    const line = text.slice(offset, nl).trim();
    offset = nl + 1;
    if (line === "" || line.startsWith("#")) continue;
    const parts = line.split(/\s+/);
    const key = parts[0].toUpperCase();
    meta[key] = parts.slice(1);
    // DATA is the last header line by definition; the body follows it.
    if (key === "DATA") {
      dataMode = (parts[1] ?? "").toLowerCase();
      break;
    }
  }
  return { meta, bodyOffset: offset, dataMode };
}

/** Scratch view for the PCL convention of storing packed rgb in a float32. */
const bitScratch = new DataView(new ArrayBuffer(4));

function unpackRgb(packed: number, into: Uint8Array, at: number): void {
  into[at] = (packed >>> 16) & 0xff;
  into[at + 1] = (packed >>> 8) & 0xff;
  into[at + 2] = packed & 0xff;
}

function readValue(view: DataView, at: number, type: FieldType, size: number): number {
  if (type === "F") return size === 8 ? view.getFloat64(at, true) : view.getFloat32(at, true);
  if (type === "U") {
    if (size === 1) return view.getUint8(at);
    if (size === 2) return view.getUint16(at, true);
    if (size === 8) return Number(view.getBigUint64(at, true));
    return view.getUint32(at, true);
  }
  if (size === 1) return view.getInt8(at);
  if (size === 2) return view.getInt16(at, true);
  if (size === 8) return Number(view.getBigInt64(at, true));
  return view.getInt32(at, true);
}

export function parsePcd(buf: ArrayBuffer): ParsedPcd {
  const bytes = new Uint8Array(buf);
  const { meta, bodyOffset, dataMode } = parseHeader(headerText(bytes));

  if (dataMode === "binary_compressed") {
    throw new Error(
      "PCD DATA binary_compressed is not supported - re-save the cloud as ascii or binary",
    );
  }
  if (dataMode !== "ascii" && dataMode !== "binary") {
    throw new Error(`PCD has no usable DATA line (found "${dataMode || "none"}")`);
  }

  const names = (meta.FIELDS ?? []).map((n) => n.toLowerCase());
  if (names.length === 0) throw new Error("PCD header has no FIELDS line");
  const sizes = meta.SIZE ?? [];
  const types = meta.TYPE ?? [];

  const fields: FieldSpec[] = [];
  let byteOffset = 0;
  let tokenOffset = 0;
  for (let i = 0; i < names.length; i++) {
    const size = Number(sizes[i] ?? 4) || 4;
    const rawType = (types[i] ?? "F").toUpperCase();
    const type: FieldType = rawType === "U" || rawType === "I" ? rawType : "F";
    const count = Math.max(1, Number(meta.COUNT?.[i] ?? 1) || 1);
    fields.push({ size, type, byteOffset, tokenOffset });
    byteOffset += size * count;
    tokenOffset += count;
  }
  const pointStep = byteOffset;
  const tokensPerPoint = tokenOffset;

  const xi = names.indexOf("x");
  const yi = names.indexOf("y");
  const zi = names.indexOf("z");
  if (xi < 0 || yi < 0 || zi < 0) {
    throw new Error(`PCD is missing x/y/z fields (has: ${names.join(" ")})`);
  }
  const ci = names.indexOf("rgb") >= 0 ? names.indexOf("rgb") : names.indexOf("rgba");

  const declared = meta.POINTS
    ? Number(meta.POINTS[0])
    : Number(meta.WIDTH?.[0] ?? 0) * Number(meta.HEIGHT?.[0] ?? 0);
  if (!Number.isFinite(declared) || declared <= 0) {
    throw new Error("PCD header declares no POINTS/WIDTH*HEIGHT count");
  }

  return dataMode === "binary"
    ? readBinary(bytes, bodyOffset, declared, pointStep, fields, xi, yi, zi, ci)
    : readAscii(bytes, bodyOffset, declared, tokensPerPoint, fields, xi, yi, zi, ci);
}

/** Points per output sample so at most MAX_POINTS survive. */
function strideFor(total: number): number {
  return Math.max(1, Math.ceil(total / MAX_POINTS));
}

function readBinary(
  bytes: Uint8Array,
  bodyOffset: number,
  declared: number,
  pointStep: number,
  fields: FieldSpec[],
  xi: number,
  yi: number,
  zi: number,
  ci: number,
): ParsedPcd {
  // A truncated file must render what it has rather than read past the end.
  const available = Math.floor((bytes.length - bodyOffset) / pointStep);
  const total = Math.max(0, Math.min(declared, available));
  const stride = strideFor(total);
  const cap = Math.ceil(total / stride);
  const points = new Float32Array(cap * 3);
  const colors = ci >= 0 ? new Uint8Array(cap * 3) : null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  let n = 0;
  for (let p = 0; p < total; p += stride) {
    const base = bodyOffset + p * pointStep;
    const fx = fields[xi];
    const fy = fields[yi];
    const fz = fields[zi];
    const x = readValue(view, base + fx.byteOffset, fx.type, fx.size);
    const y = readValue(view, base + fy.byteOffset, fy.type, fy.size);
    const z = readValue(view, base + fz.byteOffset, fz.type, fz.size);
    // Organized clouds pad missing returns with NaN; those are not geometry.
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
    points[n * 3] = x;
    points[n * 3 + 1] = y;
    points[n * 3 + 2] = z;
    if (colors) {
      const fc = fields[ci];
      const packed =
        fc.size === 4
          ? view.getUint32(base + fc.byteOffset, true)
          : readValue(view, base + fc.byteOffset, fc.type, fc.size) >>> 0;
      unpackRgb(packed, colors, n * 3);
    }
    n++;
  }
  return {
    points: points.subarray(0, n * 3),
    colors: colors ? colors.subarray(0, n * 3) : null,
  };
}

function readAscii(
  bytes: Uint8Array,
  bodyOffset: number,
  declared: number,
  tokensPerPoint: number,
  fields: FieldSpec[],
  xi: number,
  yi: number,
  zi: number,
  ci: number,
): ParsedPcd {
  const body = new TextDecoder().decode(bytes.subarray(bodyOffset));
  const lines = body.split("\n");
  const stride = strideFor(Math.min(declared, lines.length));
  const cap = Math.ceil(Math.min(declared, lines.length) / stride) + 1;
  const points = new Float32Array(cap * 3);
  const colors = ci >= 0 ? new Uint8Array(cap * 3) : null;

  let n = 0;
  let seen = 0;
  for (const raw of lines) {
    if (n >= cap) break;
    const line = raw.trim();
    if (line === "") continue;
    const keep = seen % stride === 0;
    seen++;
    if (!keep) continue;
    const tok = line.split(/\s+/);
    if (tok.length < tokensPerPoint) continue;
    const x = Number(tok[fields[xi].tokenOffset]);
    const y = Number(tok[fields[yi].tokenOffset]);
    const z = Number(tok[fields[zi].tokenOffset]);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
    points[n * 3] = x;
    points[n * 3 + 1] = y;
    points[n * 3 + 2] = z;
    if (colors) {
      const fc = fields[ci];
      const value = Number(tok[fc.tokenOffset]);
      let packed = 0;
      if (Number.isFinite(value)) {
        if (fc.type === "F") {
          // PCL writes the float whose BIT PATTERN is the packed rgb.
          bitScratch.setFloat32(0, value, true);
          packed = bitScratch.getUint32(0, true);
        } else {
          packed = value >>> 0;
        }
      }
      unpackRgb(packed, colors, n * 3);
    }
    n++;
  }
  return {
    points: points.subarray(0, n * 3),
    colors: colors ? colors.subarray(0, n * 3) : null,
  };
}
