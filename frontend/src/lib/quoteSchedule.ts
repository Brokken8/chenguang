const DAY_MS = 86_400_000;

export function chinaDateKey(date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const part = (type: string) => parts.find((value) => value.type === type)!.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** A seeded Fisher–Yates deck: repeatable on every device, without refresh rerolls. */
export function shuffledQuoteIndices(count: number): number[] {
  const deck = Array.from({ length: count }, (_, index) => index);
  let seed = 0x4d4f524e;
  for (let index = count - 1; index > 0; index--) {
    seed = (Math.imul(1664525, seed) + 1013904223) >>> 0;
    const target = Math.floor((seed / 0x100000000) * (index + 1));
    [deck[index], deck[target]] = [deck[target], deck[index]];
  }
  return deck;
}

export function dailyQuoteIndex(count: number, date = new Date()): number {
  if (!Number.isInteger(count) || count < 1) throw new Error("名言库不能为空");
  const day = Math.floor(Date.parse(`${chinaDateKey(date)}T00:00:00Z`) / DAY_MS);
  const slot = ((day % count) + count) % count;
  return shuffledQuoteIndices(count)[slot];
}
