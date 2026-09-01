#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("../../../frontend/node_modules/playwright");

async function main() {
  const outputDir = path.resolve(process.argv[2] || "docs/guides/drafts/2026-08-24");
  const screenshotDir = path.resolve(process.argv[3] || "tmp/guides/html-audit");
  fs.mkdirSync(screenshotDir, { recursive: true });
  const files = [
    "installation-guide.html",
    "user-guide.html",
    "valencia-simulation-report.html",
  ];
  const scenarios = [
    { name: "1440", width: 1440, height: 900 },
    { name: "1024", width: 1024, height: 768 },
    { name: "390", width: 390, height: 844 },
    { name: "zoom-200-equivalent", width: 720, height: 900 },
  ];
  const errors = [];
  const chromePath = process.env.BSEW_CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  try {
    for (const file of files) {
      for (const scenario of scenarios) {
        const page = await browser.newPage({ viewport: { width: scenario.width, height: scenario.height } });
        const consoleErrors = [];
        page.on("console", message => {
          if (message.type() === "error") consoleErrors.push(message.text());
        });
        await page.goto(pathToFileURL(path.join(outputDir, file)).href);
        await page.waitForLoadState("networkidle");
        const result = await page.evaluate(() => {
          const headings = [...document.querySelectorAll("h1,h2,h3,h4")].map(node => Number(node.tagName[1]));
          const links = [...document.querySelectorAll("a")].map(node => (node.textContent || "").trim());
          const regions = [...document.querySelectorAll(".table-scroll")];
          return {
            bodyOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
            h1Count: document.querySelectorAll("h1").length,
            headingSkip: headings.some((level, index) => index > 0 && level > headings[index - 1] + 1),
            emptyLinks: links.filter(label => !label).length,
            ambiguousLinks: links.filter(label => /^(click here|here|official source|link)$/i.test(label)).length,
            tableRegions: regions.length,
            tables: document.querySelectorAll("table").length,
            badTableRegions: regions.filter(node => node.getAttribute("tabindex") !== "0" || !node.getAttribute("aria-label")).length,
            bodyFontPx: Number.parseFloat(getComputedStyle(document.body).fontSize),
          };
        });
        if (result.bodyOverflow) errors.push(`${file} ${scenario.name}: page-level horizontal overflow`);
        if (result.h1Count !== 1) errors.push(`${file} ${scenario.name}: expected one H1, found ${result.h1Count}`);
        if (result.headingSkip) errors.push(`${file} ${scenario.name}: heading level skip`);
        if (result.emptyLinks || result.ambiguousLinks) errors.push(`${file} ${scenario.name}: inaccessible link labels`);
        if (result.tableRegions !== result.tables || result.badTableRegions) errors.push(`${file} ${scenario.name}: inaccessible table scroll region`);
        if (result.bodyFontPx < 16) errors.push(`${file} ${scenario.name}: body font below 16px`);
        if (consoleErrors.length) errors.push(`${file} ${scenario.name}: console errors: ${consoleErrors.join(" | ")}`);
        await page.screenshot({
          path: path.join(screenshotDir, `${path.basename(file, ".html")}-${scenario.name}.png`),
          fullPage: false,
        });
        if (scenario.name === "1440") {
          await page.emulateMedia({ media: "print" });
          await page.pdf({
            path: path.join(screenshotDir, `${path.basename(file, ".html")}-print-a4.pdf`),
            format: "A4",
            printBackground: true,
            preferCSSPageSize: true,
          });
        }
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
  if (errors.length) {
    console.error("HTML_LAYOUT_VERIFICATION_FAILED");
    for (const error of errors) console.error(`- ${error}`);
    process.exit(1);
  }
  console.log("HTML_LAYOUT_VERIFICATION_OK");
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
