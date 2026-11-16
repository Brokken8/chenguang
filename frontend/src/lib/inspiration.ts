import data from "../assets/inspiration/quotes.json";
import { dailyQuoteIndex } from "./quoteSchedule";
export { getQuotePresentation } from "./quotePresentation";

export interface DailyQuote {
  id: string;
  text: string;
  author: string;
  authorId: string;
  source: string;
  sourceUrl: string;
  original?: string;
  translation?: string;
  background: string;
  portrait: string | null;
  font?: string;
}

export const QUOTES: DailyQuote[] = data;
const imageAssets = import.meta.glob<string>("../assets/inspiration/**/*.{jpg,png,webp,svg}", {
  eager: true, query: "?url", import: "default",
});

export function inspirationImage(file: string | null): string | undefined {
  return file ? imageAssets[`../assets/inspiration/${file}`] : undefined;
}

export function getDailyQuote(date = new Date()): DailyQuote {
  return QUOTES[dailyQuoteIndex(QUOTES.length, date)];
}

