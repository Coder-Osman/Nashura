const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CR_RENDER_TEST_BROWSER || undefined});
  const base = process.env.CR_RENDER_TEST_URL || 'http://127.0.0.1:8765';
  const output = path.resolve('artifacts/render-web/deck-ui-check');
  fs.mkdirSync(output, {recursive:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [], requests = [];
    page.on('pageerror', error=>errors.push(error.message));
    // Exercise the live form without adding test recordings to the user's queue.
    await page.route('**/api/jobs', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      requests.push(route.request().postDataJSON());
      await route.fulfill({status:202, contentType:'application/json', body:'{}'});
    });
    await page.goto(base);
    await page.waitForFunction(()=>!document.getElementById('generate').disabled);
    const training = await page.locator('#deck-top optgroup[label="混合训练"] option').allTextContents();
    assert.deepEqual(training, ['石头人','巨人','野猪','皇家巨人','卫队猪']);
    assert.equal(await page.locator('#deck-top').inputValue(), 'golem-beatdown');
    await page.locator('#deck-top').selectOption('giant');
    await page.locator('#deck-bottom').selectOption('recruits-hogs');
    assert((await page.locator('#deck-top-cards').textContent()).includes('炸弹兵'));
    assert.equal(await page.locator('#deck-bottom-cards').textContent(),
      '皇家卫队、皇家野猪、飞行器、电击车小队、哥布林牢笼、火球、万箭齐发、野蛮人滚桶');
    await page.locator('#swap').click();
    assert.equal(await page.locator('#deck-top').inputValue(), 'recruits-hogs');
    assert.equal(await page.locator('#deck-bottom').inputValue(), 'giant');
    await page.locator('#generate').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/options'));
    assert.equal(requests[0].mode,'selfplay');
    assert.equal(requests[0].deck_top,'recruits-hogs');
    assert.equal(requests[0].deck_bottom,'giant');
    assert.equal(requests[0].top,requests[0].bottom);
    await page.getByText('两个权重对战', {exact:true}).click();
    const catalog = await (await page.request.get(base+'/api/checkpoints')).json();
    const topID = await page.locator('#top').inputValue();
    const top = catalog.items.find(c=>c.id===topID);
    const other = catalog.items.find(c=>c.id!==topID && c.available && c.supported && c.compatibility===top.compatibility);
    assert(other);
    await page.locator('#bottom').selectOption(other.id);
    await page.locator('#swap').click();
    assert.equal(await page.locator('#top').inputValue(),other.id);
    assert.equal(await page.locator('#bottom').inputValue(),topID);
    assert.equal(await page.locator('#deck-top').inputValue(),'giant');
    assert.equal(await page.locator('#deck-bottom').inputValue(),'recruits-hogs');
    await page.locator('#generate').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/options'));
    assert.equal(requests[1].mode,'versus');
    assert.equal(requests[1].top,other.id);
    assert.equal(requests[1].bottom,topID);
    assert.equal(requests[1].deck_top,'giant');
    assert.equal(requests[1].deck_bottom,'recruits-hogs');
    await page.locator('#refresh').click();
    await page.waitForResponse(r=>r.url().endsWith('/api/options'));
    assert.equal(await page.locator('#deck-top').inputValue(),'giant');
    for (const [name,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      await page.evaluate(()=>window.scrollTo(0,0));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.equal(await page.evaluate(()=>[...document.querySelectorAll('.deck-selection select,.deck-cards')].some(el=>el.scrollWidth>el.clientWidth)),false);
      await page.screenshot({path:path.join(output, name+'.png')});
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({trainingDecks:training, submissions:requests.length, errors, output}));
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
