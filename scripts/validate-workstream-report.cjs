#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const { pathToFileURL } = require("url");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (_) {
    const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    try {
      return require(path.join(globalRoot, "playwright"));
    } catch (_) {
      return require(path.join(globalRoot, "@playwright", "test"));
    }
  }
}

function loadAxeBuilder() {
  try {
    return require("@axe-core/playwright").default;
  } catch (_) {
    try {
      const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
      return require(path.join(globalRoot, "@axe-core", "playwright")).default;
    } catch (_) {
      return null;
    }
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function protectPrivateArtifact(filePath) {
  if (process.platform === "win32") return;
  try { fs.chmodSync(filePath, 0o600); } catch (_) { /* best-effort local privacy */ }
}

async function launchBrowser(chromium) {
  try {
    return await chromium.launch({ channel: "msedge", headless: true });
  } catch (_) {
    return chromium.launch({ headless: true });
  }
}

async function validateViewport(browser, reportUrl, outputDir, name, viewport, AxeBuilder) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.setDefaultNavigationTimeout(10000);
  const consoleErrors = [];
  page.on("console", message => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", error => consoleErrors.push(error.message));

  await page.goto(reportUrl, { waitUntil: "load" });
  await page.waitForSelector(".stream-card");
  let accessibilityViolations = [];
  if (AxeBuilder) {
    const accessibility = await new AxeBuilder({ page }).analyze();
    accessibilityViolations = accessibility.violations.filter(violation => ["critical", "serious"].includes(violation.impact));
    const accessibilityDetail = accessibilityViolations.map(violation => ({
      id: violation.id,
      impact: violation.impact,
      nodes: violation.nodes.slice(0, 5).map(node => ({ target: node.target, html: node.html, failureSummary: node.failureSummary })),
    }));
    assert(accessibilityViolations.length === 0, `${name}: serious accessibility violations: ${JSON.stringify(accessibilityDetail)}`);
  }
  const reportMeta = await page.evaluate(() => {
    const data = JSON.parse(document.getElementById("workstream-report-data").textContent);
    return {
      count: data.workstreams.length,
      previewLimit: data.issuePreviewLimit,
      firstTitle: data.workstreams[0].title,
      firstId: data.workstreams[0].id,
      firstWorkspaces: data.workstreams[0].workspaces,
      firstCheckpoints: data.workstreams[0].checkpoints,
      issueCounts: data.workstreams.map(stream => ({ id: stream.id, count: stream.issues.length })),
      machines: data.machines,
    };
  });
  assert(await page.locator(".stream-card").count() === reportMeta.count, `${name}: expected ${reportMeta.count} workstream cards`);

  const expandable = reportMeta.issueCounts.find(row => row.count > reportMeta.previewLimit);
  let initialIssues = null;
  let expandedIssues = null;
  if (expandable) {
    const expandableCard = page.locator(`.stream-card[data-stream-id="${expandable.id}"]`);
    assert(await expandableCard.count() === 1, `${name}: expected the data-declared expandable workstream`);
    initialIssues = await expandableCard.locator(".issue-row").count();
    assert(initialIssues === reportMeta.previewLimit, `${name}: expected ${reportMeta.previewLimit} default issue rows, got ${initialIssues}`);
    const issueToggle = expandableCard.locator(".issue-toggle");
    await issueToggle.click();
    expandedIssues = await expandableCard.locator(".issue-row").count();
    assert(expandedIssues === expandable.count, `${name}: issue expansion showed ${expandedIssues} of ${expandable.count} rows`);
    await expandableCard.locator(".issue-toggle").click();
    assert(await expandableCard.locator(".issue-row").count() === reportMeta.previewLimit, `${name}: issue list did not collapse back to ${reportMeta.previewLimit} rows`);
  } else {
    assert(await page.locator(".issue-toggle").count() === 0, `${name}: report offered issue expansion without more than ${reportMeta.previewLimit} issues`);
  }

  await page.locator(".resume-button").first().click();
  const dialog = page.locator("#resume-dialog");
  await dialog.waitFor({ state: "visible" });
  const dialogBounds = await dialog.evaluate(element => {
    const box = element.getBoundingClientRect();
    return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: innerWidth, height: innerHeight };
  });
  assert(dialogBounds.left >= -1, `${name}: dialog clipped on left`);
  assert(dialogBounds.top >= -1, `${name}: dialog clipped on top`);
  assert(dialogBounds.right <= dialogBounds.width + 1, `${name}: dialog clipped on right`);
  assert(dialogBounds.bottom <= dialogBounds.height + 1, `${name}: dialog clipped on bottom`);
  const dialogScreenshot = path.join(outputDir, `workstream-control-center-${name}-dialog.png`);
  await page.screenshot({ path: dialogScreenshot });
  protectPrivateArtifact(dialogScreenshot);

  const machineOptions = await page.locator("#resume-machine option").count();
  assert(machineOptions === Math.max(1, reportMeta.machines.length), `${name}: machine selector does not match report data`);
  const selectedMachineId = await page.locator("#resume-machine").inputValue();
  const selectedMachine = reportMeta.machines.find(machine => machine.id === selectedMachineId);
  const selectedHasAgent = Boolean(selectedMachine && selectedMachine.agents && selectedMachine.agents.length);
  const command = await page.locator("#resume-command").textContent();
  if (selectedHasAgent) {
    assert(command.includes("agent code bridge"), `${name}: resume command did not target Agent Bridge`);
    assert(command.includes("--project-dir") || command.includes("Resolve the exact project"), `${name}: resume payload lacks project boundary`);
  } else {
    assert(command.includes("No command generated"), `${name}: unverified machine produced an executable command`);
    assert(await page.locator("#copy-command").isDisabled(), `${name}: unavailable command remained copyable`);
  }

  const machineRows = await page.locator("#resume-machine option").evaluateAll(options => options.map(option => ({ value: option.value, text: option.textContent })));
  const windowsRows = machineRows.filter(option => option.text.toLowerCase().includes("windows"));
  const windowsMachine = windowsRows.find(option => {
    const machine = reportMeta.machines.find(row => row.id === option.value);
    if (!machine) return false;
    return !reportMeta.firstWorkspaces.some(workspace =>
      workspace.os === "windows" && workspace.machine && (workspace.machine === machine.hostname || workspace.machine === machine.id)
    );
  }) || windowsRows[0];
  if (windowsMachine) {
    await page.locator("#resume-machine").selectOption(windowsMachine.value);
    const agentValues = await page.locator("#resume-agent option").evaluateAll(options => options.map(option => option.value));
    if (agentValues.includes("grok")) await page.locator("#resume-agent").selectOption("grok");
    await page.locator("#resume-focus").fill("Verify operator's handoff");
    const windowsCommand = await page.locator("#resume-command").textContent();
    const windowsWorkspace = await page.locator("#resume-workspace").inputValue();
    const windowsMachineData = reportMeta.machines.find(machine => machine.id === windowsMachine.value) || { id: windowsMachine.value, hostname: "" };
    const exactWindowsPaths = reportMeta.firstWorkspaces
      .filter(workspace => workspace.os === "windows" && workspace.machine && (workspace.machine === windowsMachineData.hostname || workspace.machine === windowsMachineData.id))
      .map(workspace => workspace.path);
    assert(!windowsWorkspace || exactWindowsPaths.includes(windowsWorkspace), `${name}: Windows selection retained a workspace from another machine or OS`);
    if (!exactWindowsPaths.length) assert(windowsWorkspace === "", `${name}: Windows machine without exact workspace evidence was auto-filled`);
    const exactWindowsCheckpoints = reportMeta.firstCheckpoints.filter(checkpoint =>
      checkpoint.machine && (checkpoint.machine === windowsMachineData.hostname || checkpoint.machine === windowsMachineData.id)
    );
    assert((await page.locator("#checkpoint-link").isHidden()) === (exactWindowsCheckpoints.length === 0), `${name}: checkpoint link visibility does not match exact-machine evidence`);
    if (agentValues.some(Boolean)) {
      assert(windowsCommand.startsWith("& agent code bridge"), `${name}: Windows command is not explicit PowerShell syntax`);
      assert((await page.locator("#command-label").textContent()).includes("PowerShell"), `${name}: Windows command was not labeled as PowerShell`);
      assert(windowsCommand.includes("operator''s handoff"), `${name}: PowerShell single-quote escaping is invalid`);
    } else {
      assert(windowsCommand.includes("No command generated"), `${name}: Windows machine without a verified agent produced a command`);
    }
  }
  await page.locator("#resume-harness").selectOption("ledger");
  const currentAgent = await page.locator("#resume-agent").inputValue();
  if (currentAgent) {
    assert((await page.locator("#resume-command").textContent()).includes("agent code tasks create"), `${name}: task-ledger selection did not produce a ledger command`);
  } else {
    assert((await page.locator("#resume-command").textContent()).includes("No command generated"), `${name}: task ledger produced a command without a verified agent`);
  }
  await page.locator("#resume-harness").selectOption("native");
  const nativeBrief = await page.locator("#resume-command").textContent();
  assert(nativeBrief.includes("Resume workstream") && !nativeBrief.startsWith("agent code bridge"), `${name}: native selection did not produce a continuation brief`);
  if (currentAgent) await page.locator("#resume-harness").selectOption("bridge");

  await page.locator("#copy-command").click();
  await page.waitForTimeout(250);
  const toast = page.locator("#toast");
  const toastBounds = await toast.evaluate(element => {
    const box = element.getBoundingClientRect();
    return { left: box.left, top: box.top, right: box.right, bottom: box.bottom, width: innerWidth, height: innerHeight };
  });
  assert(toastBounds.left >= -1 && toastBounds.right <= toastBounds.width + 1, `${name}: toast clipped horizontally ${JSON.stringify(toastBounds)}`);
  assert(toastBounds.top >= -1 && toastBounds.bottom <= toastBounds.height + 1, `${name}: toast clipped vertically ${JSON.stringify(toastBounds)}`);
  await page.locator("#close-dialog").click();

  await page.locator("#collapse-all").click();
  assert(await page.locator(".card-body:not([hidden])").count() === 0, `${name}: collapse-all left workstreams open`);
  await page.locator("#expand-all").click();
  assert(await page.locator(".card-body:not([hidden])").count() === reportMeta.count, `${name}: expand-all did not restore all workstreams`);

  await page.locator("#search").fill(reportMeta.firstTitle);
  assert(await page.locator(".stream-card").count() >= 1, `${name}: search did not find the first workstream by title`);
  await page.locator("#search").fill("");

  const overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth }));
  assert(overflow.scrollWidth <= overflow.innerWidth + 1, `${name}: page has horizontal overflow (${overflow.scrollWidth} > ${overflow.innerWidth})`);
  assert(consoleErrors.length === 0, `${name}: browser errors: ${consoleErrors.join(" | ")}`);

  await page.waitForTimeout(2700);
  await page.evaluate(() => window.scrollTo(0, 0));
  const screenshot = path.join(outputDir, `workstream-control-center-${name}.png`);
  const viewportScreenshot = path.join(outputDir, `workstream-control-center-${name}-viewport.png`);
  await page.screenshot({ path: viewportScreenshot });
  await page.screenshot({ path: screenshot, fullPage: true });
  protectPrivateArtifact(viewportScreenshot);
  protectPrivateArtifact(screenshot);
  await context.close();
  return { name, viewport, initialIssues, expandedIssues, accessibilityViolations, dialogBounds, toastBounds, screenshot, viewportScreenshot, dialogScreenshot };
}

async function main() {
  const reportPath = path.resolve(process.argv[2] || path.join(process.env.HOME, ".local/state/agent-bridge/reports/workstream-control-center.html"));
  if (!fs.existsSync(reportPath)) throw new Error(`report not found: ${reportPath}`);
  const outputDir = path.join(path.dirname(reportPath), "validation");
  fs.mkdirSync(outputDir, { recursive: true });
  if (process.platform !== "win32") {
    try { fs.chmodSync(outputDir, 0o700); } catch (_) { /* best-effort local privacy */ }
  }
  const { chromium } = loadPlaywright();
  const AxeBuilder = loadAxeBuilder();
  const browser = await launchBrowser(chromium);
  try {
    const reportUrl = pathToFileURL(reportPath).href;
    const results = [];
    results.push(await validateViewport(browser, reportUrl, outputDir, "desktop", { width: 1440, height: 1000 }, AxeBuilder));
    results.push(await validateViewport(browser, reportUrl, outputDir, "mobile", { width: 390, height: 844 }, AxeBuilder));
    const record = { status: "passed", report: reportPath, validatedAt: new Date().toISOString(), results };
    const validationPath = path.join(outputDir, "validation.json");
    fs.writeFileSync(validationPath, JSON.stringify(record, null, 2) + "\n");
    protectPrivateArtifact(validationPath);
    process.stdout.write(JSON.stringify(record, null, 2) + "\n");
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
