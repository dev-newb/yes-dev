// Real Puppeteer client for verify_hold_live.py; no browser download or launch.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const require = createRequire(import.meta.url);
const [modulePath, browserURL, label] = process.argv.slice(2);
const imported = await import(pathToFileURL(require.resolve(modulePath)).href);
const puppeteer = imported.default ?? imported;
let browser;
try {
  browser = await puppeteer.connect({browserURL, protocolTimeout: 15000});
  const page = await browser.newPage();
  await page.goto('data:text/html,' + encodeURIComponent(
    `<title>${label}</title><p>Disposable Yes Dev probe</p>`));
  const value = await page.evaluate(() => ({title: document.title, answer: 6 * 7}));
  const session = await page.createCDPSession();
  const version = await session.send('Browser.getVersion');
  await session.detach();
  console.log(JSON.stringify({version, value, pageURL: page.url()}));
} finally {
  if (browser) await browser.disconnect();
}
