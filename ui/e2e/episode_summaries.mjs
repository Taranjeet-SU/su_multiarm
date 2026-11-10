import { mkdirSync, writeFileSync } from "node:fs";
import { chromium, expect } from "@playwright/test";
const [url, out] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
 const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
 const errors = [], csp = [];
 page.on("pageerror", e => errors.push(e.message));
 page.on("console", m => { if (m.text().includes("Content Security Policy")) csp.push(m.text()); });
 await page.goto(`${url}#/overview`);
 await expect(page.getByRole("heading", { name: "Incident overview" })).toBeVisible();
 await expect(page.locator("tbody tr").first()).toBeVisible();
 await expect(page.locator("thead th")).toHaveText(["Client / service", "Issue", "First → last observed", "Matching logs", "Observed window", "Recovery duration", "Evidence"]);
 await expect(page.getByText("Unknown", { exact: true }).first()).toBeVisible();
 await expect(page.getByText("Documents not downloaded", { exact: true })).toBeVisible();
 expect(await page.locator('input[type=date]').inputValue()).toEqual(new Date().toISOString().slice(0,10));
 const overviewRows = await page.locator("tbody tr").count();
 await page.screenshot({ path: `${out}/overview-desktop.png`, fullPage: true });
 await page.setViewportSize({ width: 390, height: 844 });
 expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(1);
 await page.screenshot({ path: `${out}/overview-mobile.png`, fullPage: true });
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.goto(`${url}#/graph`);
 await expect(page.getByRole("heading", { name: "Clients, services & issues" })).toBeVisible();
 await expect(page.getByTestId("graph-canvas")).toBeVisible();
 const graphRows = await page.getByRole("table", { name: "Client and service issue evidence" }).locator("tbody tr").count();
 expect(graphRows).toBeGreaterThan(0);
 expect(await page.getByTestId("graph-canvas").locator("canvas").count()).toBeGreaterThan(0);
 expect(await page.locator(".banner-error").count()).toBe(0);
 await page.screenshot({ path: `${out}/graph-desktop.png`, fullPage: true });
 await page.setViewportSize({ width: 390, height: 844 });
 expect(await page.evaluate(() => document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(1);
 await page.screenshot({ path: `${out}/graph-mobile.png`, fullPage: true });
 expect(errors).toEqual([]); expect(csp).toEqual([]);
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.goto(`${url}#/ask`);
 const questions = [
  ["service", "which services failed in last 3 days and what was the issue", ["Service", "Error type", "Error count"]],
  ["outage", "last 3 day outage", ["Client", "Service", "Issue", "First → last observed (UTC)", "Matching logs", "Recovery duration", "Evidence"]],
 ];
 for (const [name, question, headers] of questions) {
  await page.locator("#question").fill(question);
  const response=page.waitForResponse(r => r.url().endsWith("/v2/answer/stream"));
  await page.getByRole("button", {name:"Ask",exact:true}).click();
  const answer=page.getByTestId("answer-text");
  await expect(answer.locator("thead th")).toHaveText(headers);
  const events=(await (await response).text()).split("\n").filter(l=>l.startsWith("data: ")).map(l=>JSON.parse(l.slice(6)));
  const done=events.find(e=>e.event==="done");
  expect(done).toBeTruthy();expect(events.some(e=>e.event==="error")).toBe(false);
  expect(done.result.trace.model_calls).toEqual([]);
  if(name==="outage") expect(Object.keys(done.result.retrieval.arm_hits).sort()).toEqual(["community","graph"]);
  writeFileSync(`${out}/${name}-answer.json`,JSON.stringify(done.result,null,2));
  await page.screenshot({path:`${out}/${name}-desktop.png`,fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:`${out}/${name}-mobile.png`,fullPage:true});
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({path:`${out}/${name}-mobile.png`,fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
 }
 expect(errors).toEqual([]);expect(csp).toEqual([]);
 const result={overviewRows,graphRows,desktop:true,mobile:true,answerTables:true,modelCalls:0,pageErrors:errors,cspViolations:csp};
 writeFileSync(`${out}/browser-results.json`, JSON.stringify(result,null,2)); console.log(JSON.stringify(result));
} finally { await browser.close(); }
