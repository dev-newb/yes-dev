// Real protocol clients. Packages must be supplied from a separate QA directory.
import {createRequire} from 'node:module';
import {resolve} from 'node:path';

const [kind, endpoint, moduleDirectory] = process.argv.slice(2);
const require = createRequire(resolve(moduleDirectory, 'package.json'));
const packageName = kind === 'playwright' ? 'playwright' : 'puppeteer-core';
const library = require(packageName);
const version = require(`${packageName}/package.json`).version;
let browser;
try {
  browser = kind === 'playwright'
    ? await library.chromium.connectOverCDP(endpoint, {noDefaults: process.argv.includes('--shared')})
    : await library.connect({browserURL: endpoint});
  const page = kind === 'playwright'
    ? await browser.contexts()[0].newPage() : await browser.newPage();
  await page.goto(`data:text/html,<title>YesDev isolated ${kind}</title><p>Private test</p>`);
  const value = await page.evaluate(() => 6 * 7);
  if (value !== 42) throw new Error(`Unexpected evaluation result: ${value}`);
  await page.close();
  console.log(JSON.stringify({client: packageName, version, value, url: endpoint}));
} finally {
  if (browser) {
    if (kind === 'playwright') await browser.close();
    else browser.disconnect();
  }
}
