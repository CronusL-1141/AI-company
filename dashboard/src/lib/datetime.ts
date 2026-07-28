/**
 * 服务端时间的唯一解析与格式化入口。
 *
 * 后端全库统一 UTC（见 docs/utc-unification-design.md），响应里的时间串带
 * `+00:00` 偏移。浏览器的 `new Date(s)` 对**带偏移**的串处理得完全正确，问题
 * 出在**不带偏移**的裸串上——它会按浏览器本地时区解释，于是 UTC 的
 * `2026-07-28T09:00:00` 被读成本地 09:00，在 UTC+8 上整整偏早 8 小时。
 *
 * 这正是 ecosystem 页当年时间显示偏早 8 小时的成因，也是
 * `DeepReviewSection` 里那个只修了一个组件的 `parseAsUtc` 补丁的来历。本模块
 * 把它提升为**全站唯一解析层**：裸串一律按 UTC 读（= 存储约定），带偏移的原样解析。
 *
 * 为什么裸串不报错而是按 UTC 读：存量平移完成前库里还有裸串，浏览器也可能持有
 * 改造前的缓存响应。按 UTC 读是与存储约定唯一自洽的解释，也让新旧串在同一个
 * 函数下得到同一个答案。
 *
 * 显示一律用浏览器本地时区——传输讲 UTC，展示讲读者的钟。
 */

/** 判断串是否自带时区信息（`Z` 或 `±HH:MM` / `±HHMM` 结尾）。 */
function hasOffset(s: string): boolean {
  return /([zZ]|[+-]\d{2}:?\d{2})$/.test(s.trim());
}

/**
 * 把服务端时间串解析成 Date。无法解析（含 null/空串）时返回 null，
 * 由调用方决定如何降级——刻意不返回 Invalid Date，那种值会一路静默传播。
 */
export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;

  // 带偏移：串自己说明了口径，原样交给引擎。
  if (hasOffset(s)) {
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  // 裸串：按 UTC 读。同时把 "YYYY-MM-DD HH:MM:SS.ffffff" 归一成 ISO 形态
  // （空格换 T；Date 只认 3 位毫秒，多余的微秒位要截掉）。
  const iso = s.replace(' ', 'T').replace(/(\.\d{3})\d+$/, '$1');
  const d = new Date(`${iso}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 解析成毫秒时间戳，供排序 / 差值使用；不可解析返回 NaN。 */
export function serverTimeMs(value: string | null | undefined): number {
  return parseServerTime(value)?.getTime() ?? NaN;
}

/** 两个服务端时间串之间的毫秒差（end − start）；任一不可解析返回 NaN。 */
export function serverTimeDiffMs(
  start: string | null | undefined,
  end: string | null | undefined,
): number {
  return serverTimeMs(end) - serverTimeMs(start);
}

const DEFAULT_LOCALE = 'zh-CN';

/** 日期 + 时间，按浏览器本地时区。不可解析时原样回显，便于排查。 */
export function formatDateTime(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = {},
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleString(DEFAULT_LOCALE, options);
}

/** 只要日期。 */
export function formatDate(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = {},
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleDateString(DEFAULT_LOCALE, options);
}

/** 只要时间（默认 24 小时制——日志/时间线里 12 小时制会引入歧义）。 */
export function formatTime(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = { hour12: false },
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleTimeString(DEFAULT_LOCALE, options);
}

/**
 * 表格/时间线里最常用的紧凑形态：`2026/07/28 17:01`。
 * 抽成预设是因为它原先被五个组件各抄了一份同样的 options 字面量。
 */
export function formatDateTimeCompact(
  value: string | null | undefined,
  fallback = '—',
): string {
  return formatDateTime(
    value,
    {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    },
    fallback,
  );
}

/** 跟随系统区域设置（不强制 zh-CN），用于那些原本就用 `toLocaleString()` 的位置。 */
export function formatDateTimeSystemLocale(
  value: string | null | undefined,
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleString();
}

/** 毫秒时长 → "12s" / "3m 5s" / "2h 7m"。负数或 NaN 返回 fallback。 */
export function formatDurationMs(ms: number, fallback = '—'): string {
  if (!Number.isFinite(ms) || ms < 0) return fallback;
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

/** 两个服务端时间串之间的耗时。end 缺省表示"至今"。 */
export function formatElapsed(
  start: string | null | undefined,
  end: string | null | undefined,
  fallback = '—',
): string {
  const from = serverTimeMs(start);
  if (Number.isNaN(from)) return fallback;
  const to = end ? serverTimeMs(end) : Date.now();
  if (Number.isNaN(to)) return fallback;
  return formatDurationMs(to - from, fallback);
}

/** 距今多久（毫秒）。不可解析返回 NaN。 */
export function ageMs(value: string | null | undefined): number {
  return Date.now() - serverTimeMs(value);
}
