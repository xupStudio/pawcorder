// Persona-driven walkthrough harness.
//
// Each step is one of:
//   { goto: url }
//   { click: text-or-selector, [role]: 'button'|'link'|'textbox', [exact]: bool }
//   { fill: text-or-selector, value: '...' }
//   { press: 'Enter'|... }
//   { wait: ms }
//   { note: '...' }   // persona's inner thought, recorded in trace
//
// After every step the harness writes:
//   screenshots/<persona>/NN-<slug>.png
//   reports/<persona>-trace.md   (running log: step + what's visible after)

import { chromium } from 'playwright';
import { mkdir, writeFile, appendFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(new URL('..', import.meta.url).pathname);
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8081';

const persona = process.argv[2];
const planPath = process.argv[3];
if (!persona || !planPath) {
  console.error('usage: node harness.mjs <persona-id> <plan.json>');
  process.exit(1);
}
const plan = JSON.parse(await (await import('node:fs/promises')).readFile(planPath, 'utf8'));

const shotDir = path.join(ROOT, 'screenshots', persona);
const tracePath = path.join(ROOT, 'reports', `${persona}-trace.md`);
await mkdir(shotDir, { recursive: true });
await mkdir(path.dirname(tracePath), { recursive: true });
await writeFile(tracePath, `# ${persona} — walkthrough trace\n\n`);

const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'step';

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  viewport: { width: 390, height: 844 }, // iPhone-ish — most users on phone
  locale: plan.locale || 'zh-TW',
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();
page.setDefaultTimeout(8000);

let stepNo = 0;
async function snapshot(label) {
  stepNo += 1;
  const fname = `${String(stepNo).padStart(2, '0')}-${slug(label)}.png`;
  const fpath = path.join(shotDir, fname);
  try { await page.screenshot({ path: fpath, fullPage: true }); } catch {}
  // Dump interactable elements as the persona would scan the page.
  const visible = await page.evaluate(() => {
    const out = [];
    const isVisible = (el) => {
      const r = el.getBoundingClientRect();
      const cs = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
    };
    const text = (el) => (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 100);
    for (const sel of ['button', 'a', 'input', 'select', 'textarea', '[role=button]', '[role=link]']) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' ? 'button' : tag === 'input' ? (el.type || 'input') : tag);
        const t = text(el);
        if (!t && tag !== 'input') continue;
        out.push({ role, text: t, name: el.name || '', id: el.id || '', placeholder: el.placeholder || '' });
      }
    }
    return { url: location.href, title: document.title, headings: [...document.querySelectorAll('h1,h2,h3')].filter(h => h.offsetParent).map(h => h.innerText.trim()).slice(0, 8), bodyText: document.body.innerText.replace(/\s+/g, ' ').slice(0, 600), elements: out.slice(0, 60) };
  });
  let block = `\n## ${stepNo}. ${label}\n`;
  block += `\n- **URL**: ${visible.url}\n- **Title**: ${visible.title}\n`;
  if (visible.headings.length) block += `- **Headings**: ${visible.headings.join(' / ')}\n`;
  block += `- **Screenshot**: \`screenshots/${persona}/${fname}\`\n\n`;
  block += `**Body excerpt**: ${visible.bodyText}\n\n`;
  block += `**Interactables**:\n\n`;
  for (const e of visible.elements) {
    const desc = e.text || `(${e.placeholder || e.name || e.id || 'unnamed'})`;
    block += `- [${e.role}] ${desc}\n`;
  }
  await appendFile(tracePath, block);
  return visible;
}

async function findByText(text, role) {
  const opts = { exact: false };
  const loc = role
    ? page.getByRole(role, { name: text, ...opts })
    : page.getByText(text, opts).first();
  return loc;
}

for (const step of plan.steps) {
  if (step.note) {
    await appendFile(tracePath, `\n> 💭 *${step.note}*\n`);
    continue;
  }
  if (step.goto) {
    const url = step.goto.startsWith('http') ? step.goto : BASE + step.goto;
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(400);
    await snapshot(`goto ${step.goto}`);
    continue;
  }
  if (step.click) {
    const label = `click "${step.click}"${step.role ? ` [${step.role}]` : ''}`;
    try {
      if (step.role) {
        await page.getByRole(step.role, { name: step.click, exact: !!step.exact }).first().click();
      } else if (step.click.startsWith('css=')) {
        await page.locator(step.click.slice(4)).first().click();
      } else {
        await page.getByText(step.click, { exact: !!step.exact }).first().click();
      }
      await page.waitForTimeout(600);
      await snapshot(label);
    } catch (e) {
      await appendFile(tracePath, `\n❌ FAILED to ${label} — ${e.message.split('\n')[0]}\n`);
      await snapshot(`FAILED-${label}`);
    }
    continue;
  }
  if (step.fill) {
    try {
      const loc = step.fill.startsWith('css=')
        ? page.locator(step.fill.slice(4))
        : page.getByLabel(step.fill).or(page.getByPlaceholder(step.fill));
      await loc.first().fill(step.value);
      await snapshot(`fill "${step.fill}" = "${step.value}"`);
    } catch (e) {
      await appendFile(tracePath, `\n❌ FAILED to fill ${step.fill} — ${e.message.split('\n')[0]}\n`);
    }
    continue;
  }
  if (step.press) {
    await page.keyboard.press(step.press);
    await page.waitForTimeout(400);
    await snapshot(`press ${step.press}`);
    continue;
  }
  if (step.wait) {
    await page.waitForTimeout(step.wait);
    continue;
  }
}

await browser.close();
console.log(`done. trace -> ${tracePath}`);
