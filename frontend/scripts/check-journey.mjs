import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = fileURLToPath(new URL("../", import.meta.url));
async function loadTs(file) {
  const source = await fs.readFile(path.join(root, file), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
}
const schedule = await loadTs("src/lib/quoteSchedule.ts");
const identity = await loadTs("src/lib/identity.ts");
const presentation = await loadTs("src/lib/quotePresentation.ts");
const quotes = JSON.parse(await fs.readFile(path.join(root, "src/assets/inspiration/quotes.json"), "utf8"));
const credits = JSON.parse(await fs.readFile(path.join(root, "src/assets/inspiration/credits.json"), "utf8"));
const fonts = JSON.parse(await fs.readFile(path.join(root, "src/assets/inspiration/fonts/font-sources.json"), "utf8"));
assert.ok(quotes.length >= 40, "at least 40 quotations");
assert.equal(new Set(quotes.map((quote) => quote.id)).size, quotes.length);
assert.equal(new Set(quotes.map((quote) => quote.text)).size, quotes.length);
for (const quote of quotes) {
  const displayed = presentation.getQuotePresentation(quote);
  assert.equal(displayed.text, quote.original || quote.text, "English originals are used as the main quote");
  assert.equal(displayed.language, quote.original ? "en" : "zh-CN");
  const font = fonts.find((entry) => entry.key === displayed.font);
  assert.ok(font, "each quotation has a known font");
  await fs.access(path.join(root, "public", font.webFile));
  assert.ok(quote.author && quote.source && quote.sourceUrl.startsWith("https://"));
  await fs.access(path.join(root, "src/assets/inspiration", quote.background));
  if (quote.portrait) {
    await fs.access(path.join(root, "src/assets/inspiration", quote.portrait));
    assert.ok(credits.some((credit) => credit.file === quote.portrait && credit.license && credit.source), "portrait has attribution and license");
  }
}
const first = Date.parse("2026-01-01T04:00:00Z");
for (let start = 0; start < quotes.length * 2; start++) {
  const ids = Array.from({ length: quotes.length }, (_, index) =>
    schedule.dailyQuoteIndex(quotes.length, new Date(first + (start + index) * 86400000)));
  assert.equal(new Set(ids).size, quotes.length, "no repeats in any full deck, including across cycle boundaries");
}
assert.equal(schedule.chinaDateKey(new Date("2026-11-16T15:59:59Z")), "2026-11-16");
assert.equal(schedule.chinaDateKey(new Date("2026-11-16T16:00:00Z")), "2026-11-17");
assert.equal(schedule.dailyQuoteIndex(quotes.length, new Date("2026-11-16T01:00:00Z")),
  schedule.dailyQuoteIndex(quotes.length, new Date("2026-11-16T15:59:59Z")), "same quote all day");
assert.ok(identity.isValidIdentity({ name: "测试同学", studentId: "U202512345" }));
for (const studentId of ["U20251234", "U2025123456", "1202512345", "U203512345", "U20251A345", "u202512345", "U202５１２３４５"]) {
  assert.equal(identity.isValidIdentity({ name: "测试同学", studentId }), false, `reject ${studentId}`);
}
assert.equal(identity.isValidIdentity({ name: "  ", studentId: "U202512345" }), false);
const stored = new Map();
globalThis.sessionStorage = {
  getItem: (key) => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, value),
};
globalThis.localStorage = globalThis.sessionStorage;
assert.equal(identity.readIdentity(), null);
identity.saveIdentity({ name: "测试同学", studentId: "U202512345" });
assert.deepEqual(identity.readIdentity(), { name: "测试同学", studentId: "U202512345" });
stored.set("morning_checkin_identity", "broken json");
assert.equal(identity.readIdentity(), null, "corrupt sessions return to entrance");
console.log(`Journey checks passed: identity validation, storage, China timezone, ${quotes.length} unique quotes and all linked images.`);
