interface QuoteContent {
  text: string;
  original?: string;
  font?: string;
}

export function getQuotePresentation(quote: QuoteContent) {
  const english = Boolean(quote.original);
  return {
    text: quote.original || quote.text,
    language: english ? "en" : "zh-CN",
    font: quote.font || (english ? "serif" : "kai"),
  };
}
