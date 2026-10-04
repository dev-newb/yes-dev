// JSON-lines control for a real Puppeteer connect({browserURL}) consumer.
// CDP frames are recorded at the relay, not synthesized by this process.
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';

const [modulePath, browserURL] = process.argv.slice(2);
const {default: puppeteer} = await import(pathToFileURL(modulePath));
let browser;
const pages = new Map();
let nextPage = 0;
const input = createInterface({input: process.stdin});
for await (const line of input) {
  const request = JSON.parse(line);
  try {
    let result;
    switch (request.method) {
      case 'connect':
        browser = await puppeteer.connect({browserURL});
        result = {version: await browser.version(), pages: (await browser.pages()).length};
        break;
      case 'new_page': {
        let context = browser.defaultBrowserContext();
        if (request.params?.isolated) context = await browser.createBrowserContext();
        const page = await context.newPage();
        const id = ++nextPage;
        pages.set(id, page);
        if (request.params?.url) await page.goto(request.params.url);
        result = {id, url: page.url(), target: page.target()._targetId};
        break;
      }
      case 'work': {
        const page = pages.get(request.params.id);
        await page.goto(request.params.url);
        await page.locator('button').click();
        result = await page.evaluate(() => ({title: document.title, clicks: window.clicks, answer: 6 * 7}));
        break;
      }
      case 'pages':
        result = await Promise.all((await browser.pages()).map(async p => ({url: p.url(), title: await p.title()})));
        break;
      case 'close_page':
        await pages.get(request.params.id).close();
        result = {closed: request.params.id};
        break;
      case 'disconnect':
        await browser.disconnect();
        result = {disconnected: true};
        break;
      default:
        throw new Error(`Unknown control method ${request.method}`);
    }
    process.stdout.write(JSON.stringify({id: request.id, result}) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({id: request.id, error: String(error.stack ?? error)}) + '\n');
  }
}
if (browser?.connected) await browser.disconnect();
