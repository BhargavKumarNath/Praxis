// Screenshot every view at 1440 and 390 px, light and dark, against a running console.
//   node scripts/screenshots.mjs <outdir> [baseUrl] [view ...]
import { chromium } from "@playwright/test";

const [out = "screens", base = "http://127.0.0.1:3000", ...only] = process.argv.slice(2);
const views = only.length ? only : ["executive", "pricing", "payments", "mlops", "system", "operator"];
// Views with a detail to open: what to click, and the element to capture once it loaded.
const DETAIL = {
  pricing: { click: "[data-testid^=latest-] button", wait: "[data-testid=decision-detail] h2" },
  operator: { click: "[data-testid^=incident-] button", wait: "[data-testid=trace]" },
};
const browser = await chromium.launch();
for (const scheme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 2, colorScheme: scheme });
    for (const v of views) {
      await page.goto(`${base}/${v}`);
      await page.waitForSelector("[data-testid=read-at], [role=alert]", { timeout: 20000 });
      await page.waitForTimeout(300); // fonts and the ResizeObserver pass
      await page.screenshot({ path: `${out}/${v}-${width}-${scheme}.png`, fullPage: true });
      const open = DETAIL[v];
      if (open && (await page.locator(open.click).count())) {
        await page.locator(open.click).first().click();
        await page.waitForSelector(open.wait, { timeout: 20000 });
        await page.waitForTimeout(300);
        await page.locator(open.wait).first().locator("xpath=ancestor-or-self::section[1]").screenshot({ path: `${out}/${v}-detail-${width}-${scheme}.png` });
      }
    }
    await page.close();
  }
}
await browser.close();
